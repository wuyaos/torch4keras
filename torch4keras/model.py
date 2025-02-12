from torch import nn
import torch
from torch4keras.snippets import DottableDict, metric_mapping, get_parameter_device, info_level_prefix, print_trainable_parameters
from torch4keras.callbacks import KerasProgbar, TqdmProgbar, ProgressBar2Progbar, Callback, CallbackList, BaseLogger, History
from collections import OrderedDict
from inspect import isfunction
import os
import json
import math


class Trainer:
    """Trainer, 传入Module实例

    :param module: None/nn.Module，nn.Module()的模型实例
    """
    def __init__(self, module=None):
        super(Trainer, self).__init__()
        self.initialize(module)
    
    def initialize(self, module=None):
        # 这里主要是为了外面调用用到
        self.global_step, self.local_step, self.total_steps, self.epoch, self.steps_per_epoch, self.train_dataloader = 0, 0, 0, 0, None, None
        self.resume_step, self.resume_epoch = 0, 0
        self.retain_graph = False  # loss.backward()是否保留计算图
        self.callbacks = []
        # 传入Module实例方式
        if module is not None:
            assert isinstance(module, nn.Module), 'Args `module` only support nn.Module format'
            self.module = module
        # 是否运行Callbacks，目前主要是在DDP模式下运用
        self.run_callbacks = True

    def compile(self, loss, optimizer, scheduler=None, clip_grad_norm=None, mixed_precision=False, metrics=None, 
                stateful_metrics=None, grad_accumulation_steps=1, **kwargs):
        '''complile: 定义loss, optimizer, metrics等参数
        
        :param loss: loss
        :param optimizer: 优化器
        :param scheduler: scheduler
        :param clip_grad_norm: bool, 是否使用梯度裁剪, 默认为False
        :param mixed_precision: bool, 是否使用混合精度，默认为False
        :param metrics: str/List[str]/dict, 训练过程中需要打印的指标, loss相关指标默认会打印, 目前支持accuracy, 也支持自定义metric，形式为{key: func}
        :param stateful_metrics: List[str], 不滑动平均仅进行状态记录的metric，指标抖动会更加明显
        :param grad_accumulation_steps: int, 梯度累积步数，默认为1
        :param bar: str, 使用进度条的种类，从kwargs中解析，默认为keras, 可选keras, tqdm, progressbar2
        :return: None
        '''
        self.criterion = loss
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.clip_grad_norm = clip_grad_norm
        assert mixed_precision in {True, False, 'fp16', 'bf16'}
        self.mixed_precision = 'fp16' if mixed_precision is True else mixed_precision
        if self.mixed_precision:
            self.autocast = torch.cuda.amp.autocast
            self.scaler = torch.cuda.amp.GradScaler()

        # 训练过程观测的指标
        self.metrics = OrderedDict({'loss': None})
        if metrics is None:
            metrics = []
        elif isinstance(metrics, (str, dict)) or isfunction(metrics):
            metrics = [metrics]

        for metric in metrics:
            # 字符类型，目前仅支持accuracy
            if isinstance(metric, str) and metric != 'loss':
                self.metrics[metric] = None
            # 字典形式 {metric: func}
            elif isinstance(metric, dict):
                self.metrics.update(metric)
            # 函数形式，key和value都赋值metric
            elif isfunction(metric):
                self.metrics.update({metric: metric})
            else:
                raise ValueError('Args metrics only support `String, Dict, Callback, List[String, Dict, Callback]` format')
        self.stateful_metrics = stateful_metrics

        # 梯度累积
        self.grad_accumulation_steps = grad_accumulation_steps

        # 进度条参数
        self.bar = kwargs.get('bar', 'keras')
        assert self.bar in {'keras', 'tqdm', 'progressbar2'}, f'Args `bar`={self.bar} illegal, only support `keras, tqdm, progressbar2`'
    
    def print_trainable_parameters(self):
        """打印可训练的参数量"""
        print_trainable_parameters(self.unwrap_model())

    @property
    def device(self) -> torch.device:
        """获取model所在的device"""
        return get_parameter_device(self.unwrap_model())
        
    def to_model_device(self, *inputs, **input_kwargs):
        '''遍历并转移到model.device上'''
        # TODO
        pass

    def _forward(self, *inputs, **input_kwargs):
        # 如果传入了网络结构module，则调用module的forward
        # 如果是继承方式，则调用自身的forward
        if (len(inputs)==1) and isinstance(inputs[0], (tuple,list)):
            inputs = inputs[0]
        if isinstance(inputs, torch.Tensor):  # tensor不展开
            return self.unwrap_model().forward(inputs, **input_kwargs)
        elif isinstance(inputs, (tuple, list)):
            return self.unwrap_model().forward(*inputs, **input_kwargs)
        else:
            return self.unwrap_model().forward(inputs, **input_kwargs)

    def train_step(self, train_X, train_y):
        # 计算loss
        if self.mixed_precision:
            with self.autocast(dtype=torch.float16 if self.mixed_precision=='fp16' else torch.bfloat16):
                output = self._forward(train_X)
                loss_detail = self.criterion(output, train_y)
        else:
            output = self._forward(train_X)
            loss_detail = self.criterion(output, train_y)

        # 整理loss
        if isinstance(loss_detail, torch.Tensor):
            loss = loss_detail
            loss_detail = {}
        elif isinstance(loss_detail, dict):
            loss = loss_detail['loss']  # 还存在其他loss，仅用于打印
            del loss_detail['loss']
        elif isinstance(loss_detail, (tuple, list)):
            loss = loss_detail[0]
            loss_detail = {f'loss{i}':v for i, v in enumerate(loss_detail[1:], start=1)}
        else:
            raise ValueError('Return loss only support `Tensor/dict/tuple/list` format')

        # 梯度累积
        loss = loss / self.grad_accumulation_steps if self.grad_accumulation_steps > 1 else loss

        # loss backward
        loss = self.loss_backward(loss)
        loss_detail = {k: (v.item() if isinstance(v, torch.Tensor) else v) / self.grad_accumulation_steps for k, v in loss_detail.items()}
        loss_detail['lr'] = self.optimizer.param_groups[0]['lr']
        return output, loss, loss_detail

    def loss_backward(self, loss):
        '''loss.backward'''
        self.scale_before_step = 0
        if self.mixed_precision:  # 混合精度
            self.scale_before_step = self.scaler.get_scale()
            self.scaler.scale(loss).backward(retain_graph=self.retain_graph)
        else:
            loss.backward(retain_graph=self.retain_graph)
        return loss
    
    def step(self):
        '''参数更新'''
        skip_scheduler = False
        # 混合精度
        if self.mixed_precision:
            self.scaler.unscale_(self.optimizer)
            if self.clip_grad_norm is not None:  # 梯度裁剪
                torch.nn.utils.clip_grad_norm_(self.parameters(), self.clip_grad_norm)
            self.scaler.step(self.optimizer)
            self.scaler.update()
            skip_scheduler = self.scaler.get_scale() != self.scale_before_step
        else:
            if self.clip_grad_norm is not None:  # 梯度裁剪
                torch.nn.utils.clip_grad_norm_(self.parameters(), self.clip_grad_norm)
            self.optimizer.step()

        self.optimizer.zero_grad()  # 清梯度
        if (self.scheduler is not None) and not skip_scheduler:
            if isinstance(self.scheduler, (tuple, list)):
                for scheduler in self.scheduler:
                    scheduler.step()
            else:
                self.scheduler.step()

        # 参数裁剪
        if hasattr(self, 'clip_parameters'):
            self.clip_parameters()

    def _prepare_inputs(self, train_dataloader, steps_per_epoch, epochs, verbose, batch_size):
        '''对fit的输入进行类型检查并置为成员变量'''
        if not hasattr(train_dataloader, '__len__'):
            assert steps_per_epoch is not None, 'Either train_dataloader has attr `__len__` or steps_per_epoch is not None'
        if steps_per_epoch is None:
            self.steps_per_epoch = math.ceil(len(train_dataloader) // self.grad_accumulation_steps)
        else:
            self.steps_per_epoch = steps_per_epoch
        self.batch_size = batch_size
        self.epochs = epochs
        self.total_steps = self.steps_per_epoch * epochs
        self.train_dataloader = train_dataloader  # 设置为成员变量，可由外部的callbacks进行修改
        self.train_dataloader_iter = iter(self.train_dataloader)  # 循环epoch时不重生成
        self.verbose = self.verbose if hasattr(self, 'verbose') else verbose

    def _prepare_callbacks(self, callbacks):
        '''callbacks设置'''
        if callbacks is None:
            callbacks = []
        elif isinstance(callbacks, Callback):
            callbacks = [callbacks]
        for callback in callbacks:
            assert isinstance(callback, Callback), "Args `callbacks` only support Callback() inputs"

        history = History()
        callbacks_ = [BaseLogger(self.stateful_metrics)]

        # 进度条
        progbarlogger = None
        if self.verbose:
            if self.bar == 'keras':
                progbarlogger = KerasProgbar(stateful_metrics=self.stateful_metrics)
            elif self.bar == 'tqdm':
                progbarlogger = TqdmProgbar(stateful_metrics=self.stateful_metrics)
            elif self.bar == 'progressbar2':
                progbarlogger = ProgressBar2Progbar(stateful_metrics=self.stateful_metrics)
            else:
                progbarlogger = KerasProgbar(stateful_metrics=self.stateful_metrics)
            callbacks_.append(progbarlogger)
        callbacks_  += callbacks + [history]
        self.callbacks = CallbackList(callbacks_, run_callbacks=self.run_callbacks)
        callback_trainer = self
        callback_model = self.unwrap_model()
        params = {
            'epochs': self.epochs,
            'steps': self.steps_per_epoch,
            'verbose': self.verbose,
            'metrics': [i for i in self.metrics.keys() if isinstance(i, str)],
        }
        self.callbacks.set_all(trainer=callback_trainer, model=callback_model, optimizer=self.optimizer, scheduler=self.scheduler, params=params)
        logs = {}
        self.callbacks.on_train_begin(logs)
        callback_trainer.stop_training = False  # 在EarlyStopping中会重新设置
        return history, callback_trainer, progbarlogger

    def _prepare_nextbatch(self):
        '''准备下一个batch数据'''
        # 循环dataloader, 不要试用itertools的cycle，遇到过变量不释放的问题
        try:
            batch = next(self.train_dataloader_iter)
        except StopIteration:
            self.callbacks.on_dataloader_end()  # 适用于数据量较大时，动态读取文件并重新生成self.train_dataloader的情况，如预训练
            self.train_dataloader_iter = iter(self.train_dataloader)  # shuffle=True时候，其实顺序也重新生成了
            self.bti = 0
            batch = next(self.train_dataloader_iter)
        return batch

    def fit(self, train_dataloader, steps_per_epoch=None, epochs=1, callbacks=None, verbose=1, batch_size=None):
        '''模型训练
        
        :param train_dataloader: Dataloader, 训练数据集
        :param steps_per_epoch: int, 每个epoch训练的steps，默认为None表示自行计算 
        :param epochs: int, 训练的轮次, 默认为1
        :param callbacks: Callback/List[Callback], 回调函数，可调用预制的Callback或者自定义，默认为None 
        :param verbose: int, 是否打印，默认为1表示打印
        :return: None
        '''
        # 输入处理
        self._prepare_inputs(train_dataloader, steps_per_epoch, epochs, verbose, batch_size)

        # 准备callbacks
        history, callback_trainer, progbarlogger  = self._prepare_callbacks(callbacks)

        # epoch：当前epoch
        # global_step：当前全局训练步数
        # local_step: 当前epoch内的训练步数，不同epoch中相同local_step对应的batch数据不一定相同，在steps_per_epoch=None时相同
        # bti：在dataloader中的index，不同epoch中相同的bti对应的batch数据一般相同，除非重新生成dataloader
        self.bti = 0
        for epoch in range(self.resume_epoch, epochs):
            self.epoch = epoch
            # resume_step：判断local_step的起点，以及进度条的起始位置
            resume_step = self.resume_step if epoch==self.resume_epoch else 0
            self.callbacks.on_epoch_begin(self.global_step, self.epoch)
            if self.verbose:
                progbarlogger.seen = resume_step  # 这里设置进度条的seen，在callbacks中也会修改
            
            for local_step in range(resume_step, self.steps_per_epoch):
                self.local_step = local_step
                self.global_step = self.epoch * self.steps_per_epoch + self.local_step
                logs = self._log_init()
                self.callbacks.on_batch_begin(self.global_step, self.local_step, logs)

                # forward和backward
                self.unwrap_model().train()  # 设置为train模式
                tr_loss, tr_loss_detail = 0, {}
                for _ in range(self.grad_accumulation_steps):
                    self.train_X, self.train_y = self._prepare_nextbatch()  # 获取下一个batch的训练数据
                    self.output, self.loss, self.loss_detail = self.train_step(self.train_X, self.train_y)
                    self.callbacks.on_train_step_end()
                    tr_loss += self.loss.item()
                    for k, v in self.loss_detail.items():
                        tr_loss_detail[k] = tr_loss_detail.get(k, 0) + v
                # TODO: 理论上梯度累积时需对output和train_y进行合并，主要是为了metric_mapping计算的准确
                
                # 参数更新
                self.step()

                # 添加loss至log打印
                logs.update(dict({'loss': tr_loss}, **tr_loss_detail))
                if self.verbose and (self.global_step == resume_step):
                    progbarlogger.add_metrics(list(tr_loss_detail.keys()), add_position=1)
                    
                # 添加metrics至log打印
                for metric, func in self.metrics.items():
                    perf = metric_mapping(metric, func, self.output, self.train_y)  # 内置的一些accuracy指标
                    if perf is not None:
                        if isfunction(metric):  # 直接传入回调函数(无key)
                            if self.verbose and (self.global_step == resume_step):
                                progbarlogger.add_metrics(list(perf.keys()))
                            logs.update(perf)
                        elif isinstance(metric, str):  # 直接传入回调函数(有key)
                            logs[metric] = perf

                self.callbacks.on_batch_end(self.global_step, self.local_step, logs)

                self.bti += 1
            self.callbacks.on_epoch_end(self.global_step, self.epoch, logs)
            # TerminateOnNaN、EarlyStopping等停止训练策略
            if callback_trainer.stop_training:
                break
        self.callbacks.on_train_end(logs)
        return history

    def _log_init(self):
        '''获取batch_size，主要是用于callback中的BaseLogger和Callback
        '''
        logs = {'size': self.batch_size * self.grad_accumulation_steps}

        # 添加lr
        try:
            logs['lr'] = self.optimizer.param_groups[0]["lr"]
        except:
            pass
        return logs

    @torch.no_grad()
    def predict(self, *inputs, **input_kwargs):
        '''模型预测，调用forward()'''
        self.unwrap_model().eval()
        return self._forward(*inputs, **input_kwargs)
        
    def load_steps_params(self, save_path):
        '''导入训练过程参数
        
        :param save_path: str, 训练过程参数保存路径
        '''
        step_params = torch.load(save_path)
        self.resume_step = step_params['resume_step'] 
        self.resume_epoch = step_params['resume_epoch']
        return step_params

    def save_steps_params(self, save_path):
        '''保存训练过程参数

        :param save_path: str, 训练过程参数保存路径
        '''
        step_params = {'resume_step': (self.local_step+1) % self.steps_per_epoch, 
                       'resume_epoch': self.epoch + (self.local_step+1) // self.steps_per_epoch}
        save_dir = os.path.dirname(save_path)
        os.makedirs(save_dir, exist_ok=True)
        torch.save(step_params, save_path)

    def load_weights(self, load_path, strict=True, mapping={}):
        '''加载模型权重, 支持加载权重文件list

        :param save_path: str/tuple/list, 权重加载路径
        :param strict: bool, torch.load()是否严格加载
        :param mapping: dict, 指定key的映射
        '''
        state_dict_raw = {}
        if isinstance(load_path, (tuple, list)):
            strict = False  # 加载多个权重文件时候，strict设置为False
        elif isinstance(load_path, str):
            load_path = [load_path]
        else:
            raise ValueError('Args `load_path` only support str/tuple/list format')
        
        for load_path_i in load_path:
            state_dict = torch.load(load_path_i, map_location='cpu')
            for k, v in state_dict.items():
                k = mapping.get(k, k)
                state_dict_raw[k] = v
            self.unwrap_model().load_state_dict(state_dict_raw, strict=strict)

    def save_weights(self, save_path, mapping={}, trainable_only=False, verbose=1):
        '''保存模型权重

        :param save_path: str, 权重保存路径
        :param mapping: dict, 指定key的映射
        :param trainable_only: bool, 指定仅保存可训练参数
        '''
        state_dict_raw = {}
        state_dict = self.unwrap_model().state_dict()
        trainable_parameters = set(p for p,v in self.unwrap_model().named_parameters() if v.requires_grad)
        for k, v in state_dict.items():
            # 只保存可训练的模型部分
            if trainable_only and (k not in trainable_parameters):
                continue
            k = mapping.get(k, k)
            state_dict_raw[k] = v
        
        save_dir = os.path.dirname(save_path)
        os.makedirs(save_dir, exist_ok=True)
        torch.save(state_dict_raw, save_path)
        if trainable_only and (verbose > 0):
            params_all = sum(p.numel() for p in self.unwrap_model().parameters())
            params_trainable = sum(p.numel() for p in self.unwrap_model().parameters() if p.requires_grad)
            print(f"[INFO] Only trainable parameters saved and occupy {params_trainable}/{params_all} {params_trainable/params_all:.2f}%")

    def resume_from_checkpoint(self, model_path=None, optimizer_path=None, scheduler_path=None, step_params_path=None):
        '''同时加载模型、优化器、训练过程参数

        :param model_path: str, 模型文件路径
        :param optimizer_path: str, 优化器文件路径
        :param scheduler_path: str, scheduler文件路径
        :param step_params_path: str, 训练过程参数保存路径
        '''
        # 加载模型权重
        if model_path:
            self.load_weights(model_path)
        # 加载优化器，断点续训使用
        if optimizer_path:
            state_dict = torch.load(optimizer_path, map_location='cpu')
            self.optimizer.load_state_dict(state_dict)
        # 加载优化器，断点续训使用
        if scheduler_path:
            state_dict = torch.load(scheduler_path, map_location='cpu')
            self.scheduler.load_state_dict(state_dict)
        # 加载训练进度参数，断点续训使用
        self.load_steps_params(step_params_path)

    def save_to_checkpoint(self, model_path=None, optimizer_path=None, scheduler_path=None, step_params_path=None, mapping={}, verbose=0):
        '''同时保存模型、优化器、训练过程参数、scheduler

        :param model_path: str, 模型文件路径
        :param optimizer_path: str, 优化器文件路径
        :param scheduler_path: str, scheduler文件路径
        :param step_params_path: str, 训练过程参数保存路径
        :param mapping: dict, 模型文件的mapping
        '''
        verbose_str = ''
        if model_path:
            self.save_weights(model_path, mapping=mapping)
            verbose_str += f'Model weights successfuly saved to {model_path}.\n'
        if optimizer_path:
            save_dir = os.path.dirname(optimizer_path)
            os.makedirs(save_dir, exist_ok=True)
            torch.save(self.optimizer.state_dict(), optimizer_path)
            verbose_str += f'Optimizer successfuly saved to {optimizer_path}.\n'
        if scheduler_path and (self.scheduler is not None):
            save_dir = os.path.dirname(scheduler_path)
            os.makedirs(save_dir, exist_ok=True)
            torch.save(self.scheduler.state_dict(), scheduler_path)
            verbose_str += f'Scheduler successfuly saved to {scheduler_path}.\n'
        if step_params_path:
            self.save_steps_params(step_params_path)
            verbose_str += f'Steps_params successfuly saved to {step_params_path}.\n'
        if verbose != 0:
            print(verbose_str)

    def unwrap_model(self):
        '''返回nn.Module模块
        '''
        if isinstance(self, nn.Module): return self
        return self.module if hasattr(self, 'module') else self


class BaseModel(Trainer, nn.Module):
    """BaseModel, 使用继承的方式来使用
    """
    def __init__(self, *args, **kwargs):
        nn.Module.__init__(self)
        Trainer.__init__(self, *args, **kwargs)
        

class BaseModelDP(nn.DataParallel, BaseModel):
    '''DataParallel模式使用多gpu的方法, 父类顺序颠倒也会出问题
    '''
    def __init__(self, *args, **kwargs):
        BaseModel.__init__(self)
        nn.DataParallel.__init__(self, *args, **kwargs)


class BaseModelDDP(nn.parallel.DistributedDataParallel, BaseModel):
    '''DistributedDataParallel模式使用多gpu的方法, 父类顺序颠倒也会出问题
    '''
    def __init__(self, *args, master_rank=0, **kwargs):
        BaseModel.__init__(self)
        nn.parallel.DistributedDataParallel.__init__(self, *args, **kwargs)

        # 默认仅对master_rank=0打印信息
        assert isinstance(master_rank, (int, list, tuple)), 'Args `master_rank` only supoorts int, list, tuple'
        if isinstance(master_rank, int):
            master_rank = [master_rank]
        self.verbose = (torch.distributed.get_rank() in master_rank)


TrainerDP = BaseModelDP
TrainerDDP = BaseModelDDP


def add_trainer(obj, include=None, exclude=None):
    '''为对象添加Triner对应的方法
    '''
    include = include or []
    exclude = exclude or []
    if isinstance(include, str):
        include = [include]
    if isinstance(exclude, str):
        exclude = [exclude]
    
    if isinstance(obj, (Trainer, TrainerDP, TrainerDDP, BaseModel, BaseModelDP, BaseModelDDP)):
        return obj
    
    if isinstance(obj, nn.Module):
        import types
        for k in dir(Trainer):
            if k in include:  # 必须包含的
                pass
            elif k in exclude:  # 必须屏蔽的
                continue
            elif k.startswith('__') and k.endswith('__'):
                continue
            elif hasattr(obj, k):  # 如果下游重新定义，则不继承
                continue
           
            if eval(f'isfunction(Trainer.{k})'):
                 # 方法
                exec(f'obj.{k} = types.MethodType(Trainer.{k}, obj)')
            else:
                # TODO 属性等其他
                pass
        obj.initialize()
    return obj


class AccelerateTrainer(Trainer):
    '''accelerate来训练'''
    def __init__(self, module: nn.Module, **configs):
        super().__init__(module)
        from accelerate import Accelerator
        accelerator = Accelerator(**configs)
        self.model = accelerator.prepare(module)
        self.accelerator = accelerator
        self.device = accelerator.device
        self.verbose = 1 if accelerator.is_local_main_process else 0
        print(info_level_prefix('AcclerateTrainer may not be compatible with several callbacks, you may use custom callbacks instead.', 1))
    
    def compile(self, *args, **kwargs):
        super().compile(*args, **kwargs)
        self.optimizer, self.scheduler, self.criterion = self.accelerator.prepare(self.optimizer, self.scheduler, self.criterion)

    def _prepare_inputs(self, *args):
        super()._prepare_inputs(*args)
        self.train_dataloader = self.accelerator.prepare(self.train_dataloader)
        self.train_dataloader_iter = iter(self.train_dataloader)

    def prepare(self, *args, **kwargs):
        '''调用acclerate的prepare，如在外面评估时候需要对dev_dataloader使用'''
        return self.accelerator.prepare(*args, **kwargs)

    def unwrap_model(self):
        '''返回nn.Module模块'''
        unwrap_model = self.accelerator.unwrap_model(self.model)
        if isinstance(unwrap_model, nn.Module): return unwrap_model
        return unwrap_model.module if hasattr(unwrap_model, 'module') else unwrap_model

    def loss_backward(self, loss):
        self.accelerator.backward(loss)
        return loss


class DeepSpeedTrainer(Trainer):
    '''deepspeed来训练'''
    def __init__(self, module, config_path):
        super().__init__(module)
        self.model = add_trainer(module)
        self.config = DottableDict(json.load(open(config_path)))
        self.config['steps_per_print'] = self.config.get('steps_per_print', 1e9)  # 默认不打印，防止进度条打印问题

    def compile(self, *args, log_level='warning', inference=False, master_rank=0, **kwargs):
        super().compile(*args, **kwargs)
        import deepspeed
        from deepspeed.utils import logger as ds_logger
        import logging
        log_levels = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }

        ds_logger.setLevel(log_levels.get(log_level, logging.WARNING))

        if inference:
            # only Z3 makes sense for the inference
            info_level_prefix("ZeRO inference only makes sense with ZeRO Stage 3", 1)
            self.optimizer, self.scheduler = None, None
            model_parameters = None
        else:
            model_parameters = list(filter(lambda p: p.requires_grad, self.model.parameters()))
        
        kwargs = {
            "model": self.model,  # deepspeed的forward默认是计算到loss输出的
            "model_parameters": model_parameters,
            "config_params": self.config,
            "optimizer": self.optimizer,
            "lr_scheduler": self.scheduler,
        }
        if self.config.get('zero_optimization', {}).get('offload_optimizer', {}).get('device') == 'cpu':
            kwargs.pop('optimizer')
            print(info_level_prefix('You may not use custom optimizer when offload_optimizer=`cpu`', 1))
        self.deepspeed_engine, self.optimizer, _, self.scheduler = deepspeed.initialize(**kwargs)
        self.verbose = 1 if self.deepspeed_engine.local_rank == master_rank else 0

    def unwrap_model(self):
        # 执行deepspeed_engine的forward
        return self.deepspeed_engine

    def loss_backward(self, loss):
        self.deepspeed_engine.backward(loss)
        return loss
    
    def step(self):
        self.deepspeed_engine.step()

    @torch.inference_mode()
    def predict(self, *inputs, **input_kwargs):
        return self.deepspeed_engine.module.predict(*inputs, **input_kwargs)

    def resume_from_checkpoint(self, *args, **kwargs):
        return self.deepspeed_engine.load_checkpoint(*args, **kwargs)

    def save_to_checkpoint(self, *args, **kwargs):
        return self.deepspeed_engine.save_checkpoint(*args, **kwargs)
