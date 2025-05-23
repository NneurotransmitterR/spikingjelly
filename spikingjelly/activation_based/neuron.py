from abc import abstractmethod
from typing import Callable, Optional
import torch
import torch.nn as nn
import torch.nn.functional as F
import math
import numpy as np
import logging

from . import surrogate, base
from .auto_cuda import neuron_kernel as ac_neuron_kernel
from .auto_cuda import ss_neuron_kernel as ss_ac_neuron_kernel
try:
    import cupy
    from . import neuron_kernel, cuda_utils

except BaseException as e:
    logging.info(f'spikingjelly.activation_based.neuron: {e}')
    cupy = None
    neuron_kernel = None
    cuda_utils = None


class SimpleBaseNode(base.MemoryModule):
    def __init__(self, v_threshold: float = 1., v_reset: Optional[float] = 0.,
                 surrogate_function: Callable = surrogate.Sigmoid(), detach_reset: bool = False,
                 step_mode='s'):
        """
        A simple version of ``BaseNode``. The user can modify this neuron easily.
        """
        super().__init__()
        self.v_threshold = v_threshold
        self.v_reset = v_reset
        self.surrogate_function = surrogate_function
        self.detach_reset = detach_reset
        self.step_mode = step_mode
        self.register_memory(name='v', value=0.)

    def single_step_forward(self, x: torch.Tensor):

        self.neuronal_charge(x)
        spike = self.neuronal_fire()
        self.neuronal_reset(spike)
        return spike

    def neuronal_charge(self, x: torch.Tensor):
        raise NotImplementedError

    def neuronal_fire(self):
        return self.surrogate_function(self.v - self.v_threshold)

    def neuronal_reset(self, spike):
        if self.detach_reset:
            spike_d = spike.detach()
        else:
            spike_d = spike

        if self.v_reset is None:
            # soft reset
            self.v = self.v - self.v_threshold * spike_d

        else:
            # hard reset
            self.v = spike_d * self.v_reset + (1. - spike_d) * self.v

class SimpleIFNode(SimpleBaseNode):
    def neuronal_charge(self, x: torch.Tensor):
        self.v = self.v + x

class SimpleLIFNode(SimpleBaseNode):
    def __init__(self, tau:float, decay_input: bool, v_threshold: float = 1., v_reset: float = 0.,
                 surrogate_function: Callable = surrogate.Sigmoid(), detach_reset: bool = False,
                 step_mode='s'):
        super().__init__(v_threshold, v_reset, surrogate_function, detach_reset, step_mode)
        self.tau = tau
        self.decay_input = decay_input

    def neuronal_charge(self, x: torch.Tensor):
        if self.decay_input:
            self.v = self.v + (self.v_reset - self.v + x) / self.tau
        else:
            self.v = self.v + (self.v_reset - self.v) / self.tau + x

class BaseNode(base.MemoryModule):
    def __init__(self, v_threshold: float = 1., v_reset: Optional[float] = 0.,
                 surrogate_function: Callable = surrogate.Sigmoid(), detach_reset: bool = False,
                 step_mode='s', backend='torch', store_v_seq: bool = False):
        """
        * :ref:`API in English <BaseNode.__init__-en>`

        .. _BaseNode.__init__-cn:

        :param v_threshold: 神经元的阈值电压
        :type v_threshold: float

        :param v_reset: 神经元的重置电压。如果不为 ``None``，当神经元释放脉冲后，电压会被重置为 ``v_reset``；
            如果设置为 ``None``，当神经元释放脉冲后，电压会被减去 ``v_threshold``
        :type v_reset: Optional[float]

        :param surrogate_function: 反向传播时用来计算脉冲函数梯度的替代函数
        :type surrogate_function: Callable

        :param detach_reset: 是否将reset过程的计算图分离
        :type detach_reset: bool

        :param step_mode: 步进模式，可以为 `'s'` (单步) 或 `'m'` (多步)
        :type step_mode: str

        :param backend: 使用哪种后端。不同的 ``step_mode`` 可能会带有不同的后端。可以通过打印 ``self.supported_backends`` 查看当前
            使用的步进模式支持的后端。在支持的情况下，使用 ``'cupy'`` 后端是速度最快的
        :type backend: str

        :param store_v_seq: 在使用 ``step_mode = 'm'`` 时，给与 ``shape = [T, N, *]`` 的输入后，是否保存中间过程的 ``shape = [T, N, *]``
            的各个时间步的电压值 ``self.v_seq`` 。设置为 ``False`` 时计算完成后只保留最后一个时刻的电压，即 ``shape = [N, *]`` 的 ``self.v`` 。
            通常设置成 ``False`` ，可以节省内存
        :type store_v_seq: bool

        可微分SNN神经元的基类神经元。

        * :ref:`中文API <BaseNode.__init__-cn>`

        .. _BaseNode.__init__-en:

        :param v_threshold: threshold of this neurons layer
        :type v_threshold: float

        :param v_reset: reset voltage of this neurons layer. If not ``None``, the neuron's voltage will be set to ``v_reset``
            after firing a spike. If ``None``, the neuron's voltage will subtract ``v_threshold`` after firing a spike
        :type v_reset: Optional[float]

        :param surrogate_function: the function for calculating surrogate gradients of the heaviside step function in backward
        :type surrogate_function: Callable

        :param detach_reset: whether detach the computation graph of reset in backward
        :type detach_reset: bool

        :param step_mode: the step mode, which can be `s` (single-step) or `m` (multi-step)
        :type step_mode: str

        :param backend: backend fot this neurons layer. Different ``step_mode`` may support for different backends. The user can
        print ``self.supported_backends`` and check what backends are supported by the current ``step_mode``. If supported,
        using ``'cupy'`` backend will have the fastest training speed
        :type backend: str

        :param store_v_seq: when using ``step_mode = 'm'`` and given input with ``shape = [T, N, *]``, this option controls
            whether storing the voltage at each time-step to ``self.v_seq`` with ``shape = [T, N, *]``. If set to ``False``,
            only the voltage at last time-step will be stored to ``self.v`` with ``shape = [N, *]``, which can reduce the
            memory consumption
        :type store_v_seq: bool

        This class is the base class of differentiable spiking neurons.
        """
        assert isinstance(v_reset, float) or v_reset is None
        assert isinstance(v_threshold, float)
        assert isinstance(detach_reset, bool)
        super().__init__()

        if v_reset is None:
            self.register_memory('v', 0.)
        else:
            self.register_memory('v', v_reset)

        self.v_threshold = v_threshold
        self.v_reset = v_reset

        self.detach_reset = detach_reset
        self.surrogate_function = surrogate_function

        self.step_mode = step_mode
        self.backend = backend

        self.store_v_seq = store_v_seq

        # used in lava_exchange
        self.lava_s_cale = 1 << 6

        # used for cupy backend
        self.forward_kernel = None
        self.backward_kernel = None

    @property
    def store_v_seq(self):
        return self._store_v_seq

    @store_v_seq.setter
    def store_v_seq(self, value: bool):
        self._store_v_seq = value
        if value:
            if not hasattr(self, 'v_seq'):
                self.register_memory('v_seq', None)

    @staticmethod
    @torch.jit.script
    def jit_hard_reset(v: torch.Tensor, spike: torch.Tensor, v_reset: float):
        v = (1. - spike) * v + spike * v_reset
        return v

    @staticmethod
    @torch.jit.script
    def jit_soft_reset(v: torch.Tensor, spike: torch.Tensor, v_threshold: float):
        v = v - spike * v_threshold
        return v

    @abstractmethod
    def neuronal_charge(self, x: torch.Tensor):
        """
         * :ref:`API in English <BaseNode.neuronal_charge-en>`

        .. _BaseNode.neuronal_charge-cn:

        定义神经元的充电差分方程。子类必须实现这个函数。

        * :ref:`中文API <BaseNode.neuronal_charge-cn>`

        .. _BaseNode.neuronal_charge-en:


        Define the charge difference equation. The sub-class must implement this function.
        """
        raise NotImplementedError

    def neuronal_fire(self):
        """
        * :ref:`API in English <BaseNode.neuronal_fire-en>`

        .. _BaseNode.neuronal_fire-cn:

        根据当前神经元的电压、阈值，计算输出脉冲。

        * :ref:`中文API <BaseNode.neuronal_fire-cn>`

        .. _BaseNode.neuronal_fire-en:


        Calculate out spikes of neurons by their current membrane potential and threshold voltage.
        """

        return self.surrogate_function(self.v - self.v_threshold)

    def neuronal_reset(self, spike):
        """
        * :ref:`API in English <BaseNode.neuronal_reset-en>`

        .. _BaseNode.neuronal_reset-cn:

        根据当前神经元释放的脉冲，对膜电位进行重置。

        * :ref:`中文API <BaseNode.neuronal_reset-cn>`

        .. _BaseNode.neuronal_reset-en:


        Reset the membrane potential according to neurons' output spikes.
        """
        if self.detach_reset:
            spike_d = spike.detach()
        else:
            spike_d = spike

        if self.v_reset is None:
            # soft reset
            self.v = self.jit_soft_reset(self.v, spike_d, self.v_threshold)

        else:
            # hard reset
            self.v = self.jit_hard_reset(self.v, spike_d, self.v_reset)

    def extra_repr(self):
        return f'v_threshold={self.v_threshold}, v_reset={self.v_reset}, detach_reset={self.detach_reset}, step_mode={self.step_mode}, backend={self.backend}'

    def single_step_forward(self, x: torch.Tensor):
        """

        * :ref:`API in English <BaseNode.single_step_forward-en>`

        .. _BaseNode.single_step_forward-cn:

        :param x: 输入到神经元的电压增量
        :type x: torch.Tensor

        :return: 神经元的输出脉冲
        :rtype: torch.Tensor

        按照充电、放电、重置的顺序进行前向传播。

        * :ref:`中文API <BaseNode.single_step_forward-cn>`

        .. _BaseNode.single_step_forward-en:

        :param x: increment of voltage inputted to neurons
        :type x: torch.Tensor

        :return: out spikes of neurons
        :rtype: torch.Tensor

        Forward by the order of `neuronal_charge`, `neuronal_fire`, and `neuronal_reset`.

        """
        self.v_float_to_tensor(x)
        self.neuronal_charge(x)
        spike = self.neuronal_fire()
        self.neuronal_reset(spike)
        return spike

    def multi_step_forward(self, x_seq: torch.Tensor):
        T = x_seq.shape[0]
        y_seq = []
        if self.store_v_seq:
            v_seq = []
        for t in range(T):
            y = self.single_step_forward(x_seq[t])
            y_seq.append(y)
            if self.store_v_seq:
                v_seq.append(self.v)

        if self.store_v_seq:
            self.v_seq = torch.stack(v_seq)

        return torch.stack(y_seq)

    def v_float_to_tensor(self, x: torch.Tensor):
        if isinstance(self.v, float):
            v_init = self.v
            self.v = torch.full_like(x.data, v_init)


class AdaptBaseNode(BaseNode):
    def __init__(self, v_threshold: float = 1., v_reset: Optional[float] = 0.,
                 v_rest: float = 0., w_rest: float = 0., tau_w: float = 2., a: float = 0., b: float = 0.,
                 surrogate_function: Callable = surrogate.Sigmoid(), detach_reset: bool = False, step_mode='s',
                 backend='torch', store_v_seq: bool = False):
        # b: jump amplitudes
        # a: subthreshold coupling
        assert isinstance(w_rest, float)
        assert isinstance(v_rest, float)
        assert isinstance(tau_w, float)
        assert isinstance(a, float)
        assert isinstance(b, float)

        super().__init__(v_threshold, v_reset, surrogate_function, detach_reset, step_mode, backend, store_v_seq)

        self.register_memory('w', w_rest)

        self.w_rest = w_rest
        self.v_rest = v_rest
        self.tau_w = tau_w
        self.a = a
        self.b = b

    @staticmethod
    @torch.jit.script
    def jit_neuronal_adaptation(w: torch.Tensor, tau_w: float, a: float, v_rest: float, v: torch.Tensor):
        return w + 1. / tau_w * (a * (v - v_rest) - w)

    def neuronal_adaptation(self):
        """
        * :ref:`API in English <AdaptBaseNode.neuronal_adaptation-en>`

        .. _AdaptBaseNode.neuronal_adaptation-cn:

        脉冲触发的适应性电流的更新

        * :ref:`中文API <AdaptBaseNode.neuronal_adaptation-cn>`

        .. _AdaptBaseNode.neuronal_adaptation-en:

        Spike-triggered update of adaptation current.
        """
        self.w = self.jit_neuronal_adaptation(self.w, self.tau_w, self.a, self.v_rest, self.v)

    @staticmethod
    @torch.jit.script
    def jit_hard_reset(v: torch.Tensor, w: torch.Tensor, spike_d: torch.Tensor, v_reset: float, b: float,
                       spike: torch.Tensor):
        v = (1. - spike_d) * v + spike * v_reset
        w = w + b * spike
        return v, w

    @staticmethod
    @torch.jit.script
    def jit_soft_reset(v: torch.Tensor, w: torch.Tensor, spike_d: torch.Tensor, v_threshold: float, b: float,
                       spike: torch.Tensor):
        v = v - spike_d * v_threshold
        w = w + b * spike
        return v, w

    def neuronal_reset(self, spike):
        """
        * :ref:`API in English <AdaptBaseNode.neuronal_reset-en>`

        .. _AdaptBaseNode.neuronal_reset-cn:

        根据当前神经元释放的脉冲，对膜电位进行重置。

        * :ref:`中文API <AdaptBaseNode.neuronal_reset-cn>`

        .. _AdaptBaseNode.neuronal_reset-en:


        Reset the membrane potential according to neurons' output spikes.
        """
        if self.detach_reset:
            spike_d = spike.detach()
        else:
            spike_d = spike

        if self.v_reset is None:
            # soft reset
            self.v, self.w = self.jit_soft_reset(self.v, self.w, spike_d, self.v_threshold, self.b, spike)

        else:
            # hard reset
            self.v, self.w = self.jit_hard_reset(self.v, self.w, spike_d, self.v_reset, self.b, spike)

    def extra_repr(self):
        return super().extra_repr() + f', v_rest={self.v_rest}, w_rest={self.w_rest}, tau_w={self.tau_w}, a={self.a}, b={self.b}'

    def single_step_forward(self, x: torch.Tensor):
        self.v_float_to_tensor(x)
        self.w_float_to_tensor(x)
        self.neuronal_charge(x)
        self.neuronal_adaptation()
        spike = self.neuronal_fire()
        self.neuronal_reset(spike)
        return spike

    def w_float_to_tensor(self, x: torch.Tensor):
        if isinstance(self.w, float):
            w_init = self.w
            self.w = torch.full_like(x.data, fill_value=w_init)


class IFNode(BaseNode):
    def __init__(self, v_threshold: float = 1., v_reset: Optional[float] = 0.,
                 surrogate_function: Callable = surrogate.Sigmoid(), detach_reset: bool = False, step_mode='s',
                 backend='torch', store_v_seq: bool = False):
        """
        * :ref:`API in English <IFNode.__init__-en>`

        .. _IFNode.__init__-cn:

        :param v_threshold: 神经元的阈值电压
        :type v_threshold: float

        :param v_reset: 神经元的重置电压。如果不为 ``None``，当神经元释放脉冲后，电压会被重置为 ``v_reset``；
            如果设置为 ``None``，当神经元释放脉冲后，电压会被减去 ``v_threshold``
        :type v_reset: Optional[float]

        :param surrogate_function: 反向传播时用来计算脉冲函数梯度的替代函数
        :type surrogate_function: Callable

        :param detach_reset: 是否将reset过程的计算图分离
        :type detach_reset: bool

        :param step_mode: 步进模式，可以为 `'s'` (单步) 或 `'m'` (多步)
        :type step_mode: str

        :param backend: 使用哪种后端。不同的 ``step_mode`` 可能会带有不同的后端。可以通过打印 ``self.supported_backends`` 查看当前
            使用的步进模式支持的后端。在支持的情况下，使用 ``'cupy'`` 后端是速度最快的
        :type backend: str

        :param store_v_seq: 在使用 ``step_mode = 'm'`` 时，给与 ``shape = [T, N, *]`` 的输入后，是否保存中间过程的 ``shape = [T, N, *]``
            的各个时间步的电压值 ``self.v_seq`` 。设置为 ``False`` 时计算完成后只保留最后一个时刻的电压，即 ``shape = [N, *]`` 的 ``self.v`` 。
            通常设置成 ``False`` ，可以节省内存
        :type store_v_seq: bool

        Integrate-and-Fire 神经元模型，可以看作理想积分器，无输入时电压保持恒定，不会像LIF神经元那样衰减。其阈下神经动力学方程为：

        .. math::
            H[t] = V[t-1] + X[t]

        * :ref:`中文API <IFNode.__init__-cn>`

        .. _IFNode.__init__-en:

        :param v_threshold: threshold of this neurons layer
        :type v_threshold: float

        :param v_reset: reset voltage of this neurons layer. If not ``None``, the neuron's voltage will be set to ``v_reset``
            after firing a spike. If ``None``, the neuron's voltage will subtract ``v_threshold`` after firing a spike
        :type v_reset: Optional[float]

        :param surrogate_function: the function for calculating surrogate gradients of the heaviside step function in backward
        :type surrogate_function: Callable

        :param detach_reset: whether detach the computation graph of reset in backward
        :type detach_reset: bool

        :param step_mode: the step mode, which can be `s` (single-step) or `m` (multi-step)
        :type step_mode: str

        :param backend: backend fot this neurons layer. Different ``step_mode`` may support for different backends. The user can
        print ``self.supported_backends`` and check what backends are supported by the current ``step_mode``. If supported,
        using ``'cupy'`` backend will have the fastest training speed
        :type backend: str

        :param store_v_seq: when using ``step_mode = 'm'`` and given input with ``shape = [T, N, *]``, this option controls
            whether storing the voltage at each time-step to ``self.v_seq`` with ``shape = [T, N, *]``. If set to ``False``,
            only the voltage at last time-step will be stored to ``self.v`` with ``shape = [N, *]``, which can reduce the
            memory consumption
        :type store_v_seq: bool

        The Integrate-and-Fire neuron, which can be seen as a ideal integrator. The voltage of the IF neuron will not decay
        as that of the LIF neuron. The sub-threshold neural dynamics of it is as followed:

        .. math::
            H[t] = V[t-1] + X[t]

        """
        super().__init__(v_threshold, v_reset, surrogate_function, detach_reset, step_mode, backend, store_v_seq)

    @property
    def supported_backends(self):
        if self.step_mode == 's':
            return ('torch', 'cupy')
        elif self.step_mode == 'm':
            return ('torch', 'cupy')
        else:
            raise ValueError(self.step_mode)

    def neuronal_charge(self, x: torch.Tensor):
        self.v = self.v + x

    @staticmethod
    @torch.jit.script
    def jit_eval_single_step_forward_hard_reset(x: torch.Tensor, v: torch.Tensor, v_threshold: float, v_reset: float):
        v = v + x
        spike = (v >= v_threshold).to(x)
        v = v_reset * spike + (1. - spike) * v
        return spike, v

    @staticmethod
    @torch.jit.script
    def jit_eval_single_step_forward_soft_reset(x: torch.Tensor, v: torch.Tensor, v_threshold: float):
        v = v + x
        spike = (v >= v_threshold).to(x)
        v = v - spike * v_threshold
        return spike, v

    @staticmethod
    @torch.jit.script
    def jit_eval_multi_step_forward_hard_reset(x_seq: torch.Tensor, v: torch.Tensor, v_threshold: float,
                                               v_reset: float):
        spike_seq = torch.zeros_like(x_seq)
        for t in range(x_seq.shape[0]):
            v = v + x_seq[t]
            spike = (v >= v_threshold).to(x_seq)
            v = v_reset * spike + (1. - spike) * v
            spike_seq[t] = spike
        return spike_seq, v

    @staticmethod
    @torch.jit.script
    def jit_eval_multi_step_forward_hard_reset_with_v_seq(x_seq: torch.Tensor, v: torch.Tensor, v_threshold: float,
                                                          v_reset: float):
        spike_seq = torch.zeros_like(x_seq)
        v_seq = torch.zeros_like(x_seq)
        for t in range(x_seq.shape[0]):
            v = v + x_seq[t]
            spike = (v >= v_threshold).to(x_seq)
            v = v_reset * spike + (1. - spike) * v
            spike_seq[t] = spike
            v_seq[t] = v
        return spike_seq, v, v_seq

    @staticmethod
    @torch.jit.script
    def jit_eval_multi_step_forward_soft_reset(x_seq: torch.Tensor, v: torch.Tensor, v_threshold: float):
        spike_seq = torch.zeros_like(x_seq)
        for t in range(x_seq.shape[0]):
            v = v + x_seq[t]
            spike = (v >= v_threshold).to(x_seq)
            v = v - spike * v_threshold
            spike_seq[t] = spike
        return spike_seq, v

    @staticmethod
    @torch.jit.script
    def jit_eval_multi_step_forward_soft_reset_with_v_seq(x_seq: torch.Tensor, v: torch.Tensor, v_threshold: float):
        spike_seq = torch.zeros_like(x_seq)
        v_seq = torch.zeros_like(x_seq)
        for t in range(x_seq.shape[0]):
            v = v + x_seq[t]
            spike = (v >= v_threshold).to(x_seq)
            v = v - spike * v_threshold
            spike_seq[t] = spike
            v_seq[t] = v
        return spike_seq, v, v_seq

    def multi_step_forward(self, x_seq: torch.Tensor):
        if self.training:
            if self.backend == 'torch':
                return super().multi_step_forward(x_seq)
            elif self.backend == 'cupy':
                hard_reset = self.v_reset is not None

                if x_seq.dtype == torch.float:
                    dtype = 'float'
                elif x_seq.dtype == torch.half:
                    dtype = 'half2'
                else:
                    raise NotImplementedError(x_seq.dtype)

                if self.forward_kernel is None or not self.forward_kernel.check_attributes(hard_reset=hard_reset,
                                                                                           dtype=dtype):
                    self.forward_kernel = ac_neuron_kernel.IFNodeFPTTKernel(hard_reset=hard_reset, dtype=dtype)

                if self.backward_kernel is None or not self.backward_kernel.check_attributes(
                        surrogate_function=self.surrogate_function.cuda_codes, hard_reset=hard_reset,
                        detach_reset=self.detach_reset, dtype=dtype):
                    self.backward_kernel = ac_neuron_kernel.IFNodeBPTTKernel(
                        surrogate_function=self.surrogate_function.cuda_codes, hard_reset=hard_reset,
                        detach_reset=self.detach_reset, dtype=dtype)

                self.v_float_to_tensor(x_seq[0])

                spike_seq, v_seq = ac_neuron_kernel.IFNodeATGF.apply(x_seq.flatten(1), self.v.flatten(0),
                                                                     self.v_threshold, self.v_reset,
                                                                     self.forward_kernel,
                                                                     self.backward_kernel)

                spike_seq = spike_seq.reshape(x_seq.shape)
                v_seq = v_seq.reshape(x_seq.shape)

                if self.store_v_seq:
                    self.v_seq = v_seq

                self.v = v_seq[-1].clone()

                return spike_seq
            else:
                raise ValueError(self.backend)

        else:
            self.v_float_to_tensor(x_seq[0])
            if self.v_reset is None:
                if self.store_v_seq:
                    spike_seq, self.v, self.v_seq = self.jit_eval_multi_step_forward_soft_reset_with_v_seq(x_seq,
                                                                                                           self.v,
                                                                                                           self.v_threshold)
                else:
                    spike_seq, self.v = self.jit_eval_multi_step_forward_soft_reset(x_seq, self.v, self.v_threshold)
            else:
                if self.store_v_seq:
                    spike_seq, self.v, self.v_seq = self.jit_eval_multi_step_forward_hard_reset_with_v_seq(x_seq,
                                                                                                           self.v,
                                                                                                           self.v_threshold,
                                                                                                           self.v_reset)
                else:
                    spike_seq, self.v = self.jit_eval_multi_step_forward_hard_reset(x_seq, self.v, self.v_threshold,
                                                                                    self.v_reset)
            return spike_seq

    def single_step_forward(self, x: torch.Tensor):
        if self.training:
            if self.backend == 'torch':
                return super().single_step_forward(x)
            elif self.backend == 'cupy':
                hard_reset = self.v_reset is not None

                if x.dtype == torch.float:
                    dtype = 'float'
                elif x.dtype == torch.half:
                    dtype = 'half2'
                else:
                    raise NotImplementedError(x.dtype)
                
                if self.forward_kernel is None or not self.forward_kernel.check_attributes(hard_reset=hard_reset,
                                                                                           dtype=dtype):
                    self.forward_kernel = ss_ac_neuron_kernel.IFNodeFPKernel(hard_reset=hard_reset, dtype=dtype)

                if self.backward_kernel is None or not self.backward_kernel.check_attributes(
                        surrogate_function=self.surrogate_function.cuda_codes, hard_reset=hard_reset,
                        detach_reset=self.detach_reset, dtype=dtype):
                    self.backward_kernel = ss_ac_neuron_kernel.IFNodeBPKernel(
                        surrogate_function=self.surrogate_function.cuda_codes, hard_reset=hard_reset,
                        detach_reset=self.detach_reset, dtype=dtype)

                self.v_float_to_tensor(x)

                spike, v = ss_ac_neuron_kernel.IFNodeATGF.apply(x.flatten(0), self.v.flatten(0),
                                                                     self.v_threshold, self.v_reset,
                                                                     self.forward_kernel,
                                                                     self.backward_kernel)

                spike = spike.reshape(x.shape)
                v = v.reshape(x.shape)

                self.v = v

                return spike
            else:
                raise ValueError(self.backend)

        else:
            self.v_float_to_tensor(x)
            if self.v_reset is None:
                spike, self.v = self.jit_eval_single_step_forward_soft_reset(x, self.v, self.v_threshold)
            else:
                spike, self.v = self.jit_eval_single_step_forward_hard_reset(x, self.v, self.v_threshold, self.v_reset)
            return spike


class LIFNode(BaseNode):
    def __init__(self, tau: float = 2., decay_input: bool = True, v_threshold: float = 1.,
                 v_reset: Optional[float] = 0., surrogate_function: Callable = surrogate.Sigmoid(),
                 detach_reset: bool = False, step_mode='s', backend='torch', store_v_seq: bool = False):
        """
        * :ref:`API in English <LIFNode.__init__-en>`

        .. _LIFNode.__init__-cn:

        :param tau: 膜电位时间常数
        :type tau: float

        :param decay_input: 输入是否也会参与衰减
        :type decay_input: bool

        :param v_threshold: 神经元的阈值电压
        :type v_threshold: float

        :param v_reset: 神经元的重置电压。如果不为 ``None``，当神经元释放脉冲后，电压会被重置为 ``v_reset``；
            如果设置为 ``None``，当神经元释放脉冲后，电压会被减去 ``v_threshold``
        :type v_reset: Optional[float]

        :param surrogate_function: 反向传播时用来计算脉冲函数梯度的替代函数
        :type surrogate_function: Callable

        :param detach_reset: 是否将reset过程的计算图分离
        :type detach_reset: bool

        :param step_mode: 步进模式，可以为 `'s'` (单步) 或 `'m'` (多步)
        :type step_mode: str

        :param backend: 使用哪种后端。不同的 ``step_mode`` 可能会带有不同的后端。可以通过打印 ``self.supported_backends`` 查看当前
            使用的步进模式支持的后端。在支持的情况下，使用 ``'cupy'`` 后端是速度最快的
        :type backend: str

        :param store_v_seq: 在使用 ``step_mode = 'm'`` 时，给与 ``shape = [T, N, *]`` 的输入后，是否保存中间过程的 ``shape = [T, N, *]``
            的各个时间步的电压值 ``self.v_seq`` 。设置为 ``False`` 时计算完成后只保留最后一个时刻的电压，即 ``shape = [N, *]`` 的 ``self.v`` 。
            通常设置成 ``False`` ，可以节省内存
        :type store_v_seq: bool

        Leaky Integrate-and-Fire 神经元模型，可以看作是带漏电的积分器。其阈下神经动力学方程为：

        若 ``decay_input == True``:

            .. math::
                H[t] = V[t-1] + \\frac{1}{\\tau}(X[t] - (V[t-1] - V_{reset}))

        若 ``decay_input == False``:

            .. math::
                H[t] = V[t-1] - \\frac{1}{\\tau}(V[t-1] - V_{reset}) + X[t]


        * :ref:`中文API <LIFNode.__init__-cn>`

        .. _LIFNode.__init__-en:

        :param tau: membrane time constant
        :type tau: float

        :param decay_input: whether the input will decay
        :type decay_input: bool

        :param v_threshold: threshold of this neurons layer
        :type v_threshold: float

        :param v_reset: reset voltage of this neurons layer. If not ``None``, the neuron's voltage will be set to ``v_reset``
            after firing a spike. If ``None``, the neuron's voltage will subtract ``v_threshold`` after firing a spike
        :type v_reset: Optional[float]

        :param surrogate_function: the function for calculating surrogate gradients of the heaviside step function in backward
        :type surrogate_function: Callable

        :param detach_reset: whether detach the computation graph of reset in backward
        :type detach_reset: bool

        :param step_mode: the step mode, which can be `s` (single-step) or `m` (multi-step)
        :type step_mode: str

        :param backend: backend fot this neurons layer. Different ``step_mode`` may support for different backends. The user can
        print ``self.supported_backends`` and check what backends are supported by the current ``step_mode``. If supported,
        using ``'cupy'`` backend will have the fastest training speed
        :type backend: str

        :param store_v_seq: when using ``step_mode = 'm'`` and given input with ``shape = [T, N, *]``, this option controls
            whether storing the voltage at each time-step to ``self.v_seq`` with ``shape = [T, N, *]``. If set to ``False``,
            only the voltage at last time-step will be stored to ``self.v`` with ``shape = [N, *]``, which can reduce the
            memory consumption
        :type store_v_seq: bool

        The Leaky Integrate-and-Fire neuron, which can be seen as a leaky integrator.
        The subthreshold neural dynamics of it is as followed:

        IF ``decay_input == True``:

            .. math::
                H[t] = V[t-1] + \\frac{1}{\\tau}(X[t] - (V[t-1] - V_{reset}))

        IF ``decay_input == False``:

            .. math::
                H[t] = V[t-1] - \\frac{1}{\\tau}(V[t-1] - V_{reset}) + X[t]

        """
        assert isinstance(tau, float) and tau > 1.

        super().__init__(v_threshold, v_reset, surrogate_function, detach_reset, step_mode, backend, store_v_seq)

        self.tau = tau
        self.decay_input = decay_input

    @property
    def supported_backends(self):
        if self.step_mode == 's':
            return ('torch', 'cupy')
        elif self.step_mode == 'm':
            return ('torch', 'cupy')
        else:
            raise ValueError(self.step_mode)

    def extra_repr(self):
        return super().extra_repr() + f', tau={self.tau}'

    def neuronal_charge(self, x: torch.Tensor):
        if self.decay_input:
            if self.v_reset is None or self.v_reset == 0.:
                self.v = self.neuronal_charge_decay_input_reset0(x, self.v, self.tau)
            else:
                self.v = self.neuronal_charge_decay_input(x, self.v, self.v_reset, self.tau)

        else:
            if self.v_reset is None or self.v_reset == 0.:
                self.v = self.neuronal_charge_no_decay_input_reset0(x, self.v, self.tau)
            else:
                self.v = self.neuronal_charge_no_decay_input(x, self.v, self.v_reset, self.tau)

    @staticmethod
    @torch.jit.script
    def neuronal_charge_decay_input_reset0(x: torch.Tensor, v: torch.Tensor, tau: float):
        v = v + (x - v) / tau
        return v

    @staticmethod
    @torch.jit.script
    def neuronal_charge_decay_input(x: torch.Tensor, v: torch.Tensor, v_reset: float, tau: float):
        v = v + (x - (v - v_reset)) / tau
        return v

    @staticmethod
    @torch.jit.script
    def neuronal_charge_no_decay_input_reset0(x: torch.Tensor, v: torch.Tensor, tau: float):
        v = v * (1. - 1. / tau) + x
        return v

    @staticmethod
    @torch.jit.script
    def neuronal_charge_no_decay_input(x: torch.Tensor, v: torch.Tensor, v_reset: float, tau: float):
        v = v - (v - v_reset) / tau + x
        return v

    @staticmethod
    @torch.jit.script
    def jit_eval_single_step_forward_hard_reset_decay_input(x: torch.Tensor, v: torch.Tensor, v_threshold: float,
                                                            v_reset: float, tau: float):
        v = v + (x - (v - v_reset)) / tau
        spike = (v >= v_threshold).to(x)
        v = v_reset * spike + (1. - spike) * v
        return spike, v

    @staticmethod
    @torch.jit.script
    def jit_eval_single_step_forward_hard_reset_no_decay_input(x: torch.Tensor, v: torch.Tensor, v_threshold: float,
                                                               v_reset: float, tau: float):
        v = v - (v - v_reset) / tau + x
        spike = (v >= v_threshold).to(x)
        v = v_reset * spike + (1. - spike) * v
        return spike, v

    @staticmethod
    @torch.jit.script
    def jit_eval_single_step_forward_soft_reset_decay_input(x: torch.Tensor, v: torch.Tensor, v_threshold: float,
                                                            tau: float):
        v = v + (x - v) / tau
        spike = (v >= v_threshold).to(x)
        v = v - spike * v_threshold
        return spike, v

    @staticmethod
    @torch.jit.script
    def jit_eval_single_step_forward_soft_reset_no_decay_input(x: torch.Tensor, v: torch.Tensor, v_threshold: float,
                                                               tau: float):
        v = v * (1. - 1. / tau) + x
        spike = (v >= v_threshold).to(x)
        v = v - spike * v_threshold
        return spike, v

    @staticmethod
    @torch.jit.script
    def jit_eval_multi_step_forward_hard_reset_decay_input(x_seq: torch.Tensor, v: torch.Tensor, v_threshold: float,
                                                           v_reset: float, tau: float):
        spike_seq = torch.zeros_like(x_seq)
        for t in range(x_seq.shape[0]):
            v = v + (x_seq[t] - (v - v_reset)) / tau
            spike = (v >= v_threshold).to(x_seq)
            v = v_reset * spike + (1. - spike) * v
            spike_seq[t] = spike
        return spike_seq, v

    @staticmethod
    @torch.jit.script
    def jit_eval_multi_step_forward_hard_reset_decay_input_with_v_seq(x_seq: torch.Tensor, v: torch.Tensor,
                                                                      v_threshold: float, v_reset: float, tau: float):
        spike_seq = torch.zeros_like(x_seq)
        v_seq = torch.zeros_like(x_seq)
        for t in range(x_seq.shape[0]):
            v = v + (x_seq[t] - (v - v_reset)) / tau
            spike = (v >= v_threshold).to(x_seq)
            v = v_reset * spike + (1. - spike) * v
            spike_seq[t] = spike
            v_seq[t] = v
        return spike_seq, v, v_seq

    @staticmethod
    @torch.jit.script
    def jit_eval_multi_step_forward_hard_reset_no_decay_input(x_seq: torch.Tensor, v: torch.Tensor, v_threshold: float,
                                                              v_reset: float, tau: float):
        spike_seq = torch.zeros_like(x_seq)
        for t in range(x_seq.shape[0]):
            v = v - (v - v_reset) / tau + x_seq[t]
            spike = (v >= v_threshold).to(x_seq)
            v = v_reset * spike + (1. - spike) * v
            spike_seq[t] = spike
        return spike_seq, v

    @staticmethod
    @torch.jit.script
    def jit_eval_multi_step_forward_hard_reset_no_decay_input_with_v_seq(x_seq: torch.Tensor, v: torch.Tensor,
                                                                         v_threshold: float, v_reset: float,
                                                                         tau: float):
        spike_seq = torch.zeros_like(x_seq)
        v_seq = torch.zeros_like(x_seq)
        for t in range(x_seq.shape[0]):
            v = v - (v - v_reset) / tau + x_seq[t]
            spike = (v >= v_threshold).to(x_seq)
            v = v_reset * spike + (1. - spike) * v
            spike_seq[t] = spike
            v_seq[t] = v
        return spike_seq, v, v_seq

    @staticmethod
    @torch.jit.script
    def jit_eval_multi_step_forward_soft_reset_decay_input(x_seq: torch.Tensor, v: torch.Tensor, v_threshold: float,
                                                           tau: float):
        spike_seq = torch.zeros_like(x_seq)
        for t in range(x_seq.shape[0]):
            v = v + (x_seq[t] - v) / tau
            spike = (v >= v_threshold).to(x_seq)
            v = v - spike * v_threshold
            spike_seq[t] = spike
        return spike_seq, v

    @staticmethod
    @torch.jit.script
    def jit_eval_multi_step_forward_soft_reset_decay_input_with_v_seq(x_seq: torch.Tensor, v: torch.Tensor,
                                                                      v_threshold: float, tau: float):
        spike_seq = torch.zeros_like(x_seq)
        v_seq = torch.zeros_like(x_seq)
        for t in range(x_seq.shape[0]):
            v = v + (x_seq[t] - v) / tau
            spike = (v >= v_threshold).to(x_seq)
            v = v - spike * v_threshold
            spike_seq[t] = spike
            v_seq[t] = v
        return spike_seq, v, v_seq

    @staticmethod
    @torch.jit.script
    def jit_eval_multi_step_forward_soft_reset_no_decay_input(x_seq: torch.Tensor, v: torch.Tensor, v_threshold: float,
                                                              tau: float):
        spike_seq = torch.zeros_like(x_seq)
        for t in range(x_seq.shape[0]):
            v = v * (1. - 1. / tau) + x_seq[t]
            spike = (v >= v_threshold).to(x_seq)
            v = v - spike * v_threshold
            spike_seq[t] = spike
        return spike_seq, v

    @staticmethod
    @torch.jit.script
    def jit_eval_multi_step_forward_soft_reset_no_decay_input_with_v_seq(x_seq: torch.Tensor, v: torch.Tensor,
                                                                         v_threshold: float,
                                                                         tau: float):
        spike_seq = torch.zeros_like(x_seq)
        v_seq = torch.zeros_like(x_seq)
        for t in range(x_seq.shape[0]):
            v = v * (1. - 1. / tau) + x_seq[t]
            spike = (v >= v_threshold).to(x_seq)
            v = v - spike * v_threshold
            spike_seq[t] = spike
            v_seq[t] = v
        return spike_seq, v, v_seq

    def single_step_forward(self, x: torch.Tensor):
        if self.training:
            if self.backend == 'torch':
                return super().single_step_forward(x)
            elif self.backend == 'cupy':
                hard_reset = self.v_reset is not None

                if x.dtype == torch.float:
                    dtype = 'float'
                elif x.dtype == torch.half:
                    dtype = 'half2'
                else:
                    raise NotImplementedError(x.dtype)
                
                if self.forward_kernel is None or not self.forward_kernel.check_attributes(hard_reset=hard_reset,
                                                                                           dtype=dtype,
                                                                                           decay_input=self.decay_input):
                    self.forward_kernel = ss_ac_neuron_kernel.LIFNodeFPKernel(decay_input=self.decay_input,
                                                                              hard_reset=hard_reset, dtype=dtype)

                if self.backward_kernel is None or not self.backward_kernel.check_attributes(
                        surrogate_function=self.surrogate_function.cuda_codes, hard_reset=hard_reset,
                        detach_reset=self.detach_reset, dtype=dtype, decay_input=self.decay_input):
                    self.backward_kernel = ss_ac_neuron_kernel.LIFNodeBPKernel(
                        decay_input=self.decay_input,
                        surrogate_function=self.surrogate_function.cuda_codes, hard_reset=hard_reset,
                        detach_reset=self.detach_reset, dtype=dtype)

                self.v_float_to_tensor(x)

                spike, v = ss_ac_neuron_kernel.LIFNodeATGF.apply(x.flatten(0), self.v.flatten(0),
                                                                 self.v_threshold, self.v_reset, 1. / self.tau,
                                                                 self.forward_kernel,
                                                                 self.backward_kernel)

                spike = spike.reshape(x.shape)
                v = v.reshape(x.shape)

                self.v = v

                return spike
            else:
                raise ValueError(self.backend)

        else:
            self.v_float_to_tensor(x)
            if self.v_reset is None:
                if self.decay_input:
                    spike, self.v = self.jit_eval_single_step_forward_soft_reset_decay_input(x, self.v,
                                                                                             self.v_threshold, self.tau)
                else:
                    spike, self.v = self.jit_eval_single_step_forward_soft_reset_no_decay_input(x, self.v,
                                                                                                self.v_threshold,
                                                                                                self.tau)
            else:
                if self.decay_input:
                    spike, self.v = self.jit_eval_single_step_forward_hard_reset_decay_input(x, self.v,
                                                                                             self.v_threshold,
                                                                                             self.v_reset, self.tau)
                else:
                    spike, self.v = self.jit_eval_single_step_forward_hard_reset_no_decay_input(x, self.v,
                                                                                                self.v_threshold,
                                                                                                self.v_reset,
                                                                                                self.tau)
            return spike

    def multi_step_forward(self, x_seq: torch.Tensor):
        if self.training:
            if self.backend == 'torch':
                return super().multi_step_forward(x_seq)
            elif self.backend == 'cupy':

                hard_reset = self.v_reset is not None
                if x_seq.dtype == torch.float:
                    dtype = 'float'
                elif x_seq.dtype == torch.half:
                    dtype = 'half2'
                else:
                    raise NotImplementedError(x_seq.dtype)

                if self.forward_kernel is None or not self.forward_kernel.check_attributes(hard_reset=hard_reset,
                                                                                           dtype=dtype,
                                                                                           decay_input=self.decay_input):
                    self.forward_kernel = ac_neuron_kernel.LIFNodeFPTTKernel(decay_input=self.decay_input,
                                                                             hard_reset=hard_reset, dtype=dtype)

                if self.backward_kernel is None or not self.backward_kernel.check_attributes(
                        surrogate_function=self.surrogate_function.cuda_codes, hard_reset=hard_reset,
                        detach_reset=self.detach_reset, dtype=dtype, decay_input=self.decay_input):
                    self.backward_kernel = ac_neuron_kernel.LIFNodeBPTTKernel(decay_input=self.decay_input,
                                                                              surrogate_function=self.surrogate_function.cuda_codes,
                                                                              hard_reset=hard_reset,
                                                                              detach_reset=self.detach_reset,
                                                                              dtype=dtype)

                self.v_float_to_tensor(x_seq[0])

                spike_seq, v_seq = ac_neuron_kernel.LIFNodeATGF.apply(x_seq.flatten(1), self.v.flatten(0),
                                                                      self.v_threshold, self.v_reset, 1. / self.tau,
                                                                      self.forward_kernel,
                                                                      self.backward_kernel)

                spike_seq = spike_seq.reshape(x_seq.shape)
                v_seq = v_seq.reshape(x_seq.shape)

                if self.store_v_seq:
                    self.v_seq = v_seq

                self.v = v_seq[-1].clone()

                return spike_seq
            else:
                raise ValueError(self.backend)

        else:
            self.v_float_to_tensor(x_seq[0])
            if self.v_reset is None:
                if self.decay_input:
                    if self.store_v_seq:
                        spike_seq, self.v, self.v_seq = self.jit_eval_multi_step_forward_soft_reset_decay_input_with_v_seq(
                            x_seq, self.v, self.v_threshold, self.tau)
                    else:
                        spike_seq, self.v = self.jit_eval_multi_step_forward_soft_reset_decay_input(x_seq, self.v,
                                                                                                    self.v_threshold,
                                                                                                    self.tau)
                else:
                    if self.store_v_seq:
                        spike_seq, self.v, self.v_seq = self.jit_eval_multi_step_forward_soft_reset_no_decay_input_with_v_seq(
                            x_seq, self.v, self.v_threshold, self.tau)
                    else:
                        spike_seq, self.v = self.jit_eval_multi_step_forward_soft_reset_no_decay_input(x_seq, self.v,
                                                                                                       self.v_threshold,
                                                                                                       self.tau)
            else:
                if self.decay_input:
                    if self.store_v_seq:
                        spike_seq, self.v, self.v_seq = self.jit_eval_multi_step_forward_hard_reset_decay_input_with_v_seq(
                            x_seq, self.v, self.v_threshold, self.v_reset, self.tau)
                    else:
                        spike_seq, self.v = self.jit_eval_multi_step_forward_hard_reset_decay_input(x_seq, self.v,
                                                                                                    self.v_threshold,
                                                                                                    self.v_reset,
                                                                                                    self.tau)
                else:
                    if self.store_v_seq:
                        spike_seq, self.v, self.v_seq = self.jit_eval_multi_step_forward_hard_reset_no_decay_input_with_v_seq(
                            x_seq, self.v, self.v_threshold, self.v_reset, self.tau)
                    else:
                        spike_seq, self.v = self.jit_eval_multi_step_forward_hard_reset_no_decay_input(x_seq, self.v,
                                                                                                       self.v_threshold,
                                                                                                       self.v_reset,
                                                                                                       self.tau)

            return spike_seq


class ParametricLIFNode(BaseNode):
    def __init__(self, init_tau: float = 2.0, decay_input: bool = True, v_threshold: float = 1.,
                 v_reset: Optional[float] = 0., surrogate_function: Callable = surrogate.Sigmoid(),
                 detach_reset: bool = False, step_mode='s', backend='torch', store_v_seq: bool = False):
        """
        * :ref:`API in English <ParametricLIFNode.__init__-en>`

        .. _ParametricLIFNode.__init__-cn:

        :param init_tau: 膜电位时间常数的初始值
        :type init_tau: float

        :param decay_input: 输入是否也会参与衰减
        :type decay_input: bool

        :param v_threshold: 神经元的阈值电压
        :type v_threshold: float

        :param v_reset: 神经元的重置电压。如果不为 ``None``，当神经元释放脉冲后，电压会被重置为 ``v_reset``；
            如果设置为 ``None``，当神经元释放脉冲后，电压会被减去 ``v_threshold``
        :type v_reset: Optional[float]

        :param surrogate_function: 反向传播时用来计算脉冲函数梯度的替代函数
        :type surrogate_function: Callable

        :param detach_reset: 是否将reset过程的计算图分离
        :type detach_reset: bool

        :param step_mode: 步进模式，可以为 `'s'` (单步) 或 `'m'` (多步)
        :type step_mode: str

        :param backend: 使用哪种后端。不同的 ``step_mode`` 可能会带有不同的后端。可以通过打印 ``self.supported_backends`` 查看当前
            使用的步进模式支持的后端。在支持的情况下，使用 ``'cupy'`` 后端是速度最快的
        :type backend: str

        :param store_v_seq: 在使用 ``step_mode = 'm'`` 时，给与 ``shape = [T, N, *]`` 的输入后，是否保存中间过程的 ``shape = [T, N, *]``
            的各个时间步的电压值 ``self.v_seq`` 。设置为 ``False`` 时计算完成后只保留最后一个时刻的电压，即 ``shape = [N, *]`` 的 ``self.v`` 。
            通常设置成 ``False`` ，可以节省内存
        :type store_v_seq: bool

        :param cupy_fp32_inference: 若为 `True`，在 `eval` 模式下，使用float32，却在GPU上运行，并且 `cupy` 已经安装，则会自动使用 `cupy` 进行加速。
            这个选项的优先权高于 ``backend``
        :type cupy_fp32_inference: bool

        `Incorporating Learnable Membrane Time Constant to Enhance Learning of Spiking Neural Networks <https://arxiv.org/abs/2007.05785>`_
        提出的 Parametric Leaky Integrate-and-Fire (PLIF)神经元模型，可以看作是带漏电的积分器。其阈下神经动力学方程为：

        若 ``decay_input == True``:

            .. math::
                H[t] = V[t-1] + \\frac{1}{\\tau}(X[t] - (V[t-1] - V_{reset}))

        若 ``decay_input == False``:

            .. math::
                H[t] = V[t-1] - \\frac{1}{\\tau}(V[t-1] - V_{reset}) + X[t]

        其中 :math:`\\frac{1}{\\tau} = {\\rm Sigmoid}(w)`，:math:`w` 是可学习的参数。

        * :ref:`中文API <ParametricLIFNode.__init__-cn>`

        .. _ParametricLIFNode.__init__-en:

        :param init_tau: the initial value of membrane time constant
        :type init_tau: float

        :param decay_input: whether the input will decay
        :type decay_input: bool

        :param v_threshold: threshold of this neurons layer
        :type v_threshold: float

        :param v_reset: reset voltage of this neurons layer. If not ``None``, the neuron's voltage will be set to ``v_reset``
            after firing a spike. If ``None``, the neuron's voltage will subtract ``v_threshold`` after firing a spike
        :type v_reset: Optional[float]

        :param surrogate_function: the function for calculating surrogate gradients of the heaviside step function in backward
        :type surrogate_function: Callable

        :param detach_reset: whether detach the computation graph of reset in backward
        :type detach_reset: bool

        :param step_mode: the step mode, which can be `s` (single-step) or `m` (multi-step)
        :type step_mode: str

        :param backend: backend fot this neurons layer. Different ``step_mode`` may support for different backends. The user can
        print ``self.supported_backends`` and check what backends are supported by the current ``step_mode``. If supported,
        using ``'cupy'`` backend will have the fastest training speed
        :type backend: str

        :param store_v_seq: when using ``step_mode = 'm'`` and given input with ``shape = [T, N, *]``, this option controls
            whether storing the voltage at each time-step to ``self.v_seq`` with ``shape = [T, N, *]``. If set to ``False``,
            only the voltage at last time-step will be stored to ``self.v`` with ``shape = [N, *]``, which can reduce the
            memory consumption
        :type store_v_seq: bool

        :param cupy_fp32_inference: If `True`, if this module is in `eval` mode, using float32, running on GPU, and `cupy` is installed, then this
            module will use `cupy` to accelerate. This option has priority over ``backend``
        :type cupy_fp32_inference: bool

        The Parametric Leaky Integrate-and-Fire (PLIF) neuron, which is proposed by `Incorporating Learnable Membrane Time Constant to Enhance Learning of Spiking Neural Networks <https://arxiv.org/abs/2007.05785>`_ and can be seen as a leaky integrator.
        The subthreshold neural dynamics of it is as followed:

        IF ``decay_input == True``:

            .. math::
                H = V[t-1] + \\frac{1}{\\tau}(X[t] - (V[t-1] - V_{reset}))

        IF ``decay_input == False``:

            .. math::
                H[t] = V[t-1] - \\frac{1}{\\tau}(V[t-1] - V_{reset}) + X[t]

        where :math:`\\frac{1}{\\tau} = {\\rm Sigmoid}(w)`, :math:`w` is a learnable parameter.
        """

        assert isinstance(init_tau, float) and init_tau > 1.
        super().__init__(v_threshold, v_reset, surrogate_function, detach_reset, step_mode, backend, store_v_seq)
        self.decay_input = decay_input
        init_w = - math.log(init_tau - 1.)
        self.w = nn.Parameter(torch.as_tensor(init_w))

    @property
    def supported_backends(self):
        if self.step_mode == 's':
            return ('torch',)
        elif self.step_mode == 'm':
            return ('torch', 'cupy')
        else:
            raise ValueError(self.step_mode)

    def extra_repr(self):
        with torch.no_grad():
            tau = 1. / self.w.sigmoid()
        return super().extra_repr() + f', tau={tau}'

    def neuronal_charge(self, x: torch.Tensor):
        if self.decay_input:
            if self.v_reset is None or self.v_reset == 0.:
                self.v = self.v + (x - self.v) * self.w.sigmoid()
            else:
                self.v = self.v + (x - (self.v - self.v_reset)) * self.w.sigmoid()
        else:
            if self.v_reset is None or self.v_reset == 0.:
                self.v = self.v * (1. - self.w.sigmoid()) + x
            else:
                self.v = self.v - (self.v - self.v_reset) * self.w.sigmoid() + x

    def multi_step_forward(self, x_seq: torch.Tensor):
        if self.backend == 'torch':
            return super().multi_step_forward(x_seq)
        elif self.backend == 'cupy':
            hard_reset = self.v_reset is not None
            if x_seq.dtype == torch.float:
                dtype = 'float'
            elif x_seq.dtype == torch.half:
                dtype = 'half2'
            else:
                raise NotImplementedError(x_seq.dtype)

            if self.forward_kernel is None or not self.forward_kernel.check_attributes(hard_reset=hard_reset,
                                                                                       dtype=dtype,
                                                                                       decay_input=self.decay_input):
                self.forward_kernel = ac_neuron_kernel.ParametricLIFNodeFPTTKernel(decay_input=self.decay_input,
                                                                                   hard_reset=hard_reset, dtype=dtype)

            if self.backward_kernel is None or not self.backward_kernel.check_attributes(
                    surrogate_function=self.surrogate_function.cuda_codes, hard_reset=hard_reset,
                    detach_reset=self.detach_reset, dtype=dtype, decay_input=self.decay_input):
                self.backward_kernel = ac_neuron_kernel.ParametricLIFNodeBPTTKernel(decay_input=self.decay_input,
                                                                                    surrogate_function=self.surrogate_function.cuda_codes,
                                                                                    hard_reset=hard_reset,
                                                                                    detach_reset=self.detach_reset,
                                                                                    dtype=dtype)

            self.v_float_to_tensor(x_seq[0])

            spike_seq, v_seq = ac_neuron_kernel.ParametricLIFNodeATGF.apply(
                x_seq.flatten(1), self.v.flatten(0), self.v_threshold, self.v_reset, self.w.sigmoid().to(x_seq),
                self.forward_kernel, self.backward_kernel)

            spike_seq = spike_seq.reshape(x_seq.shape)
            v_seq = v_seq.reshape(x_seq.shape)

            if self.store_v_seq:
                self.v_seq = v_seq

            self.v = v_seq[-1].clone()

            return spike_seq
        else:
            raise ValueError(self.backend)


class QIFNode(BaseNode):
    def __init__(self, tau: float = 2., v_c: float = 0.8, a0: float = 1., v_threshold: float = 1., v_rest: float = 0.,
                 v_reset: Optional[float] = -0.1,
                 surrogate_function: Callable = surrogate.Sigmoid(), detach_reset: bool = False, step_mode='s',
                 backend='torch', store_v_seq: bool = False):
        """
        * :ref:`API in English <QIFNode.__init__-en>`

        .. _QIFNode.__init__-cn:

        :param tau: 膜电位时间常数
        :type tau: float

        :param v_c: 关键电压
        :type v_c: float

        :param a0:
        :type a0: float

        :param v_threshold: 神经元的阈值电压
        :type v_threshold: float

        :param v_rest: 静息电位
        :type v_rest: float

        :param v_reset: 神经元的重置电压。如果不为 ``None``，当神经元释放脉冲后，电压会被重置为 ``v_reset``；
            如果设置为 ``None``，当神经元释放脉冲后，电压会被减去 ``v_threshold``
        :type v_reset: Optional[float]

        :param surrogate_function: 反向传播时用来计算脉冲函数梯度的替代函数
        :type surrogate_function: Callable

        :param detach_reset: 是否将reset过程的计算图分离
        :type detach_reset: bool

        :param step_mode: 步进模式，可以为 `'s'` (单步) 或 `'m'` (多步)
        :type step_mode: str

        :param backend: 使用哪种后端。不同的 ``step_mode`` 可能会带有不同的后端。可以通过打印 ``self.supported_backends`` 查看当前
            使用的步进模式支持的后端。在支持的情况下，使用 ``'cupy'`` 后端是速度最快的
        :type backend: str

        :param store_v_seq: 在使用 ``step_mode = 'm'`` 时，给与 ``shape = [T, N, *]`` 的输入后，是否保存中间过程的 ``shape = [T, N, *]``
            的各个时间步的电压值 ``self.v_seq`` 。设置为 ``False`` 时计算完成后只保留最后一个时刻的电压，即 ``shape = [N, *]`` 的 ``self.v`` 。
            通常设置成 ``False`` ，可以节省内存
        :type store_v_seq: bool


        Quadratic Integrate-and-Fire 神经元模型，一种非线性积分发放神经元模型，也是指数积分发放神经元(Exponential Integrate-and-Fire)的近似版本。其阈下神经动力学方程为：

        .. math::
            H[t] = V[t-1] + \\frac{1}{\\tau}(X[t] + a_0 (V[t-1] - V_{rest})(V[t-1] - V_c))

        * :ref:`中文API <QIFNode.__init__-cn>`

        .. _QIFNode.__init__-en:

        :param tau: membrane time constant
        :type tau: float

        :param v_c: critical voltage
        :type v_c: float

        :param a0:
        :type a0: float

        :param v_threshold: threshold voltage of neurons
        :type v_threshold: float

        :param v_reset: reset voltage of this neurons layer. If not ``None``, the neuron's voltage will be set to ``v_reset``
            after firing a spike. If ``None``, the neuron's voltage will subtract ``v_threshold`` after firing a spike
        :type v_reset: Optional[float]

        :param surrogate_function: the function for calculating surrogate gradients of the heaviside step function in backward
        :type surrogate_function: Callable

        :param detach_reset: whether detach the computation graph of reset in backward
        :type detach_reset: bool

        :param step_mode: the step mode, which can be `s` (single-step) or `m` (multi-step)
        :type step_mode: str

        :param backend: backend fot this neurons layer. Different ``step_mode`` may support for different backends. The user can
        print ``self.supported_backends`` and check what backends are supported by the current ``step_mode``. If supported,
        using ``'cupy'`` backend will have the fastest training speed
        :type backend: str

        :param store_v_seq: when using ``step_mode = 'm'`` and given input with ``shape = [T, N, *]``, this option controls
            whether storing the voltage at each time-step to ``self.v_seq`` with ``shape = [T, N, *]``. If set to ``False``,
            only the voltage at last time-step will be stored to ``self.v`` with ``shape = [N, *]``, which can reduce the
            memory consumption
        :type store_v_seq: bool

        The Quadratic Integrate-and-Fire neuron is a kind of nonlinear integrate-and-fire models and also an approximation of the Exponential Integrate-and-Fire model.
        The subthreshold neural dynamics of it is as followed:

        .. math::
            H[t] = V[t-1] + \\frac{1}{\\tau}(X[t] + a_0 (V[t-1] - V_{rest})(V[t-1] - V_c))
        """

        assert isinstance(tau, float) and tau > 1.
        if v_reset is not None:
            assert v_threshold > v_reset
            assert v_rest >= v_reset
        assert a0 > 0

        super().__init__(v_threshold, v_reset, surrogate_function, detach_reset, step_mode, backend, store_v_seq)
        self.tau = tau
        self.v_c = v_c
        self.v_rest = v_rest
        self.a0 = a0

    def extra_repr(self):
        return super().extra_repr() + f', tau={self.tau}, v_c={self.v_c}, a0={self.a0}, v_rest={self.v_rest}'

    def neuronal_charge(self, x: torch.Tensor):
        self.v = self.v + (x + self.a0 * (self.v - self.v_rest) * (self.v - self.v_c)) / self.tau

    @property
    def supported_backends(self):
        if self.step_mode == 's':
            return ('torch',)
        elif self.step_mode == 'm':
            return ('torch', 'cupy')
        else:
            raise ValueError(self.step_mode)

    def multi_step_forward(self, x_seq: torch.Tensor):
        if self.backend == 'torch':
            return super().multi_step_forward(x_seq)
        elif self.backend == 'cupy':
            self.v_float_to_tensor(x_seq[0])

            spike_seq, v_seq = neuron_kernel.MultiStepQIFNodePTT.apply(
                x_seq.flatten(1), self.v.flatten(0), self.tau, self.v_threshold, self.v_reset, self.v_rest,
                self.v_c, self.a0, self.detach_reset, self.surrogate_function.cuda_code)

            spike_seq = spike_seq.reshape(x_seq.shape)
            v_seq = v_seq.reshape(x_seq.shape)

            if self.store_v_seq:
                self.v_seq = v_seq

            self.v = v_seq[-1].clone()

            return spike_seq
        else:
            raise ValueError(self.backend)


class EIFNode(BaseNode):
    def __init__(self, tau: float = 2., delta_T: float = 1., theta_rh: float = .8, v_threshold: float = 1.,
                 v_rest: float = 0., v_reset: Optional[float] = -0.1,
                 surrogate_function: Callable = surrogate.Sigmoid(), detach_reset: bool = False, step_mode='s',
                 backend='torch', store_v_seq: bool = False):
        """
        * :ref:`API in English <EIFNode.__init__-en>`

        .. _EIFNode.__init__-cn:

        :param tau: 膜电位时间常数
        :type tau: float

        :param delta_T: 陡峭度参数
        :type delta_T: float

        :param theta_rh: 基强度电压阈值
        :type theta_rh: float

        :param v_threshold: 神经元的阈值电压
        :type v_threshold: float

        :param v_reset: 神经元的重置电压。如果不为 ``None``，当神经元释放脉冲后，电压会被重置为 ``v_reset``；
            如果设置为 ``None``，当神经元释放脉冲后，电压会被减去 ``v_threshold``
        :type v_reset: Optional[float]

        :param surrogate_function: 反向传播时用来计算脉冲函数梯度的替代函数
        :type surrogate_function: Callable

        :param detach_reset: 是否将reset过程的计算图分离
        :type detach_reset: bool

        :param step_mode: 步进模式，可以为 `'s'` (单步) 或 `'m'` (多步)
        :type step_mode: str

        :param backend: 使用哪种后端。不同的 ``step_mode`` 可能会带有不同的后端。可以通过打印 ``self.supported_backends`` 查看当前
            使用的步进模式支持的后端。在支持的情况下，使用 ``'cupy'`` 后端是速度最快的
        :type backend: str

        :param store_v_seq: 在使用 ``step_mode = 'm'`` 时，给与 ``shape = [T, N, *]`` 的输入后，是否保存中间过程的 ``shape = [T, N, *]``
            的各个时间步的电压值 ``self.v_seq`` 。设置为 ``False`` 时计算完成后只保留最后一个时刻的电压，即 ``shape = [N, *]`` 的 ``self.v`` 。
            通常设置成 ``False`` ，可以节省内存
        :type store_v_seq: bool


        Exponential Integrate-and-Fire 神经元模型，一种非线性积分发放神经元模型，是由HH神经元模型(Hodgkin-Huxley model)简化后推导出的一维模型。在 :math:`\\Delta_T\\to 0` 时退化为LIF模型。其阈下神经动力学方程为：

        .. math::
            H[t] = V[t-1] + \\frac{1}{\\tau}\\left(X[t] - (V[t-1] - V_{rest}) + \\Delta_T\\exp\\left(\\frac{V[t-1] - \\theta_{rh}}{\\Delta_T}\\right)\\right)

        * :ref:`中文API <EIFNode.__init__-cn>`

        .. _EIFNode.__init__-en:

        :param tau: membrane time constant
        :type tau: float

        :param delta_T: sharpness parameter
        :type delta_T: float

        :param theta_rh: rheobase threshold
        :type theta_rh: float

        :param v_threshold: threshold of this neurons layer
        :type v_threshold: float

        :param v_reset: reset voltage of this neurons layer. If not ``None``, the neuron's voltage will be set to ``v_reset``
            after firing a spike. If ``None``, the neuron's voltage will subtract ``v_threshold`` after firing a spike
        :type v_reset: Optional[float]

        :param surrogate_function: the function for calculating surrogate gradients of the heaviside step function in backward
        :type surrogate_function: Callable

        :param detach_reset: whether detach the computation graph of reset in backward
        :type detach_reset: bool

        :param step_mode: the step mode, which can be `s` (single-step) or `m` (multi-step)
        :type step_mode: str

        :param backend: backend fot this neurons layer. Different ``step_mode`` may support for different backends. The user can
        print ``self.supported_backends`` and check what backends are supported by the current ``step_mode``. If supported,
        using ``'cupy'`` backend will have the fastest training speed
        :type backend: str

        :param store_v_seq: when using ``step_mode = 'm'`` and given input with ``shape = [T, N, *]``, this option controls
            whether storing the voltage at each time-step to ``self.v_seq`` with ``shape = [T, N, *]``. If set to ``False``,
            only the voltage at last time-step will be stored to ``self.v`` with ``shape = [N, *]``, which can reduce the
            memory consumption
        :type store_v_seq: bool

        The Exponential Integrate-and-Fire neuron is a kind of nonlinear integrate-and-fire models and also an one-dimensional model derived from the Hodgkin-Huxley model. It degenerates to the LIF model when :math:`\\Delta_T\\to 0`.
        The subthreshold neural dynamics of it is as followed:

        .. math::
            H[t] = V[t-1] + \\frac{1}{\\tau}\\left(X[t] - (V[t-1] - V_{rest}) + \\Delta_T\\exp\\left(\\frac{V[t-1] - \\theta_{rh}}{\\Delta_T}\\right)\\right)
        """

        assert isinstance(tau, float) and tau > 1.
        if v_reset is not None:
            assert v_threshold > v_reset
            assert v_rest >= v_reset
        assert delta_T > 0

        super().__init__(v_threshold, v_reset, surrogate_function, detach_reset, step_mode, backend, store_v_seq)
        self.tau = tau
        self.delta_T = delta_T
        self.v_rest = v_rest
        self.theta_rh = theta_rh

    def extra_repr(self):
        return super().extra_repr() + f', tau={self.tau}, delta_T={self.delta_T}, theta_rh={self.theta_rh}'

    def neuronal_charge(self, x: torch.Tensor):
        with torch.no_grad():
            if not isinstance(self.v, torch.Tensor):
                self.v = torch.as_tensor(self.v, device=x.device)

        self.v = self.v + (x + self.v_rest - self.v + self.delta_T * torch.exp(
            (self.v - self.theta_rh) / self.delta_T)) / self.tau

    @property
    def supported_backends(self):
        if self.step_mode == 's':
            return ('torch',)
        elif self.step_mode == 'm':
            return ('torch', 'cupy')
        else:
            raise ValueError(self.step_mode)

    def multi_step_forward(self, x_seq: torch.Tensor):
        if self.backend == 'torch':
            return super().multi_step_forward(x_seq)
        elif self.backend == 'cupy':
            self.v_float_to_tensor(x_seq[0])

            spike_seq, v_seq = neuron_kernel.MultiStepEIFNodePTT.apply(
                x_seq.flatten(1), self.v.flatten(0), self.tau, self.v_threshold, self.v_reset, self.v_rest,
                self.theta_rh, self.delta_T, self.detach_reset, self.surrogate_function.cuda_code)

            spike_seq = spike_seq.reshape(x_seq.shape)
            v_seq = v_seq.reshape(x_seq.shape)

            if self.store_v_seq:
                self.v_seq = v_seq

            self.v = v_seq[-1].clone()

            return spike_seq
        else:
            raise ValueError(self.backend)


class IzhikevichNode(AdaptBaseNode):
    def __init__(self, tau: float = 2., v_c: float = 0.8, a0: float = 1., v_threshold: float = 1.,
                 v_reset: Optional[float] = 0., v_rest: float = -0.1, w_rest: float = 0., tau_w: float = 2., a: float = 0.,
                 b: float = 0.,
                 surrogate_function: Callable = surrogate.Sigmoid(), detach_reset: bool = False, step_mode='s',
                 backend='torch', store_v_seq: bool = False):
        assert isinstance(tau, float) and tau > 1.
        assert a0 > 0

        super().__init__(v_threshold, v_reset, v_rest, w_rest, tau_w, a, b, surrogate_function, detach_reset, step_mode,
                         backend, store_v_seq)
        self.tau = tau
        self.v_c = v_c
        self.a0 = a0

    def extra_repr(self):
        return super().extra_repr() + f', tau={self.tau}, v_c={self.v_c}, a0={self.a0}'

    def neuronal_charge(self, x: torch.Tensor):
        self.v = self.v + (x + self.a0 * (self.v - self.v_rest) * (self.v - self.v_c) - self.w) / self.tau

    @property
    def supported_backends(self):
        if self.step_mode == 's':
            return ('torch',)
        elif self.step_mode == 'm':
            return ('torch', 'cupy')
        else:
            raise ValueError(self.step_mode)

    def multi_step_forward(self, x_seq: torch.Tensor):
        if self.backend == 'torch':
            return super().multi_step_forward(x_seq)
        elif self.backend == 'cupy':
            self.v_float_to_tensor(x_seq[0])
            self.w_float_to_tensor(x_seq[0])

            spike_seq, v_seq, w_seq = neuron_kernel.MultiStepIzhikevichNodePTT.apply(
                x_seq.flatten(1), self.v.flatten(0), self.w.flatten(0), self.tau, self.v_threshold, self.v_reset,
                self.v_rest, self.a, self.b, self.tau_w,
                self.v_c, self.a0, self.detach_reset, self.surrogate_function.cuda_code)

            spike_seq = spike_seq.reshape(x_seq.shape)
            v_seq = v_seq.reshape(x_seq.shape)
            w_seq = w_seq.reshape(x_seq.shape)

            if self.store_v_seq:
                self.v_seq = v_seq

            self.v = v_seq[-1].clone()
            self.w = w_seq[-1].clone()

            return spike_seq
        else:
            raise ValueError(self.backend)


class LIAFNode(LIFNode):
    def __init__(self, act: Callable, threshold_related: bool, *args, **kwargs):
        """
        * :ref:`API in English <LIAFNode.__init__-en>`

        .. _LIAFNode.__init__-cn:

        :param act: 激活函数
        :type act: Callable
        :param threshold_related: 是否使用阈值依赖模式 (TR mode). 若为 ``True`` 则 ``y = act(h - v_th)``，
            否则 ``y = act(h)``
        :type threshold_related: bool

        `LIAF-Net: Leaky Integrate and Analog Fire Network for Lightweight and Efficient Spatiotemporal Information Processing <https://arxiv.org/abs/2011.06176>`_ 提出的LIAF神经元。LIAFNode和LIFNode的行为相同，但输出是 ``self.act(...)`` 而非脉冲。

        .. Warning::

            The outputs of this neurons layer are not binary spikes.


        * :ref:`中文API <LIAFNode.__init__-cn>`

        .. _LIAFNode.__init__-en:

        :param act: the activation function
        :type act: Callable
        :param threshold_related: whether the neuron uses threshold related (TR mode). If ``True``, ``y = act(h - v_th)``,
            otherwise ``y = act(h)``
        :type threshold_related: bool

        Other parameters in `*args, **kwargs` are same with :class:`LIFNode`.

        The LIAF neuron proposed in `LIAF-Net: Leaky Integrate and Analog Fire Network for Lightweight and Efficient Spatiotemporal Information Processing <https://arxiv.org/abs/2011.06176>`_. LIAFNode has the same behavior as LIFNode, but outputs ``self.act(...)``
        rather than spikes.

        .. admonition:: Warning
            :class: warning

            The outputs of this neurons layer are not binary spikes.

        """
        super().__init__(*args, **kwargs)
        self.act = act
        self.threshold_related = threshold_related

        assert self.backend == 'torch', "LIAFNode only supports for backend='torch'!"
        assert self.single_step_cupy_fp32_inference == False, "LIAFNode does not support for single_step_cupy_fp32_inference!"

    @property
    def supported_backends(self):
        return ('torch',)

    def single_step_forward(self, x: torch.Tensor):
        self.neuronal_charge(x)
        if self.threshold_related:
            y = self.act(self.v - self.v_threshold)
        else:
            y = self.act(self.v)
        spike = self.neuronal_fire()
        self.neuronal_reset(spike)
        return y


class KLIFNode(BaseNode):
    def __init__(self, scale_reset: bool = False, tau: float = 2., decay_input: bool = True, v_threshold: float = 1.,
                 v_reset: Optional[float] = 0., surrogate_function: Callable = surrogate.Sigmoid(),
                 detach_reset: bool = False, step_mode='s', backend='torch', store_v_seq: bool = False):
        """
        * :ref:`API in English <KLIFNode.__init__-en>`

        .. _KLIFNode.__init__-cn:

        :param scale_reset: 是否在 ``neuronal_reset`` 时将 ``v`` 进行缩放
        :type scale_reset: bool

        :param tau: 膜电位时间常数
        :type tau: float

        :param decay_input: 输入是否也会参与衰减
        :type decay_input: bool

        :param v_threshold: 神经元的阈值电压
        :type v_threshold: float

        :param v_reset: 神经元的重置电压。如果不为 ``None``，当神经元释放脉冲后，电压会被重置为 ``v_reset``；
            如果设置为 ``None``，当神经元释放脉冲后，电压会被减去 ``v_threshold``
        :type v_reset: Optional[float]

        :param surrogate_function: 反向传播时用来计算脉冲函数梯度的替代函数
        :type surrogate_function: Callable

        :param detach_reset: 是否将reset过程的计算图分离
        :type detach_reset: bool

        :param step_mode: 步进模式，可以为 `'s'` (单步) 或 `'m'` (多步)
        :type step_mode: str

        :param backend: 使用哪种后端。不同的 ``step_mode`` 可能会带有不同的后端。可以通过打印 ``self.supported_backends`` 查看当前
            使用的步进模式支持的后端。在支持的情况下，使用 ``'cupy'`` 后端是速度最快的
        :type backend: str

        :param store_v_seq: 在使用 ``step_mode = 'm'`` 时，给与 ``shape = [T, N, *]`` 的输入后，是否保存中间过程的 ``shape = [T, N, *]``
            的各个时间步的电压值 ``self.v_seq`` 。设置为 ``False`` 时计算完成后只保留最后一个时刻的电压，即 ``shape = [N, *]`` 的 ``self.v`` 。
            通常设置成 ``False`` ，可以节省内存
        :type store_v_seq: bool

        `KLIF: An optimized spiking neuron unit for tuning surrogate gradient slope and membrane potential <https://arxiv.org/abs/2302.09238>`_ 提出的K-based Leaky Integrate-and-Fire 神经元模型，可以看作是带漏电的积分器。其阈下神经动力学方程为：

        若 ``decay_input == True``:

            .. math::
                H[t] = V[t-1] + \\frac{1}{\\tau}(X[t] - (V[t-1] - V_{reset}))

        若 ``decay_input == False``:

            .. math::
                H[t] = V[t-1] - \\frac{1}{\\tau}(V[t-1] - V_{reset}) + X[t]

        注意，KLIF神经元的放电和重置与普通的神经元不同，为：

            .. math::

                F[t] &= \\mathrm{ReLU}(kH[t])

                S[t] &= \\Theta(F[t] - V_{th})

        如果 ``scale_reset == False``，则

            .. math::
                V[t] = \\begin{cases}
                    F[t](1-S[t]) + V_{reset}S[t], hard~~reset \\\\
                    F[t] - S[t]V_{th}, soft~~reset
                \\end{cases}

        如果 ``scale_reset == True``，则

            .. math::
                V[t] = \\begin{cases}
                    \\frac{F[t]}{k}(1-S[t]) + V_{reset}S[t], hard~~reset \\\\
                    \\frac{1}{k}(F[t] - S[t]V_{th}), soft~~reset
                \\end{cases}



        * :ref:`中文API <KLIFNode.__init__-cn>`

        .. _KLIFNode.__init__-en:

        :param scale_reset: whether scale ``v`` in ``neuronal_reset``
        :type scale_reset: bool

        :param tau: membrane time constant
        :type tau: float

        :param decay_input: whether the input will decay
        :type decay_input: bool

        :param v_threshold: threshold of this neurons layer
        :type v_threshold: float

        :param v_reset: reset voltage of this neurons layer. If not ``None``, the neuron's voltage will be set to ``v_reset``
            after firing a spike. If ``None``, the neuron's voltage will subtract ``v_threshold`` after firing a spike
        :type v_reset: Optional[float]

        :param surrogate_function: the function for calculating surrogate gradients of the heaviside step function in backward
        :type surrogate_function: Callable

        :param detach_reset: whether detach the computation graph of reset in backward
        :type detach_reset: bool

        :param step_mode: the step mode, which can be `s` (single-step) or `m` (multi-step)
        :type step_mode: str

        :param backend: backend fot this neurons layer. Different ``step_mode`` may support for different backends. The user can
        print ``self.supported_backends`` and check what backends are supported by the current ``step_mode``. If supported,
        using ``'cupy'`` backend will have the fastest training speed
        :type backend: str

        :param store_v_seq: when using ``step_mode = 'm'`` and given input with ``shape = [T, N, *]``, this option controls
            whether storing the voltage at each time-step to ``self.v_seq`` with ``shape = [T, N, *]``. If set to ``False``,
            only the voltage at last time-step will be stored to ``self.v`` with ``shape = [N, *]``, which can reduce the
            memory consumption
        :type store_v_seq: bool

        The K-based Leaky Integrate-and-Fire neuron proposed by `KLIF: An optimized spiking neuron unit for tuning surrogate gradient slope and membrane potential <https://arxiv.org/abs/2302.09238>`_, which can be seen as a leaky integrator.
        The subthreshold neural dynamics of it is as followed:

        IF ``decay_input == True``:

            .. math::
                H[t] = V[t-1] + \\frac{1}{\\tau}(X[t] - (V[t-1] - V_{reset}))

        IF ``decay_input == False``:

            .. math::
                H[t] = V[t-1] - \\frac{1}{\\tau}(V[t-1] - V_{reset}) + X[t]

        Note that the neuronal fire and reset of the KLIF neuron is different from native neurons:

            .. math::

                F[t] &= \\mathrm{ReLU}(kH[t])

                S[t] &= \\Theta(F[t] - V_{th})

        If ``scale_reset == False``, then

            .. math::
                V[t] = \\begin{cases}
                    F[t](1-S[t]) + V_{reset}S[t], hard~~reset \\\\
                    F[t] - S[t]V_{th}, soft~~reset
                \\end{cases}

        Elif ``scale_reset == True``, then

            .. math::
                V[t] = \\begin{cases}
                    \\frac{F[t]}{k}(1-S[t]) + V_{reset}S[t], hard~~reset \\\\
                    \\frac{1}{k}(F[t] - S[t]V_{th}), soft~~reset
                \\end{cases}


        """
        assert isinstance(tau, float) and tau > 1.
        if backend == 'cupy':
            raise NotImplementedError("The CuPy backend for the KLIF neuron has not been implemented!")

        super().__init__(v_threshold, v_reset, surrogate_function, detach_reset, step_mode, backend, store_v_seq)

        self.scale_reset = scale_reset
        self.tau = tau
        self.decay_input = decay_input

        self.k = nn.Parameter(torch.as_tensor(1.))

    @staticmethod
    @torch.jit.script
    def neuronal_charge_decay_input(x: torch.Tensor, v: torch.Tensor, v_reset: float, tau: float, k: torch.Tensor):
        v = v + (x - (v - v_reset)) / tau
        v = torch.relu_(k * v)
        return v

    @staticmethod
    @torch.jit.script
    def neuronal_charge_no_decay_input(x: torch.Tensor, v: torch.Tensor, v_reset: float, tau: float, k: torch.Tensor):
        v = v - (v - v_reset) / tau + x
        v = torch.relu_(k * v)
        return v

    def neuronal_charge(self, x: torch.Tensor):
        if self.v_reset is None:
            v_reset = 0.
        else:
            v_reset = self.v_reset
        if self.decay_input:
            self.v = self.neuronal_charge_decay_input(x, self.v, v_reset, self.tau, self.k)

        else:
            self.v = self.neuronal_charge_no_decay_input(x, self.v, v_reset, self.tau, self.k)

    def neuronal_reset(self, spike):
        if self.detach_reset:
            spike_d = spike.detach()
        else:
            spike_d = spike

        if self.scale_reset:
            if self.v_reset is None:
                # soft reset
                self.v = self.jit_soft_reset(self.v, spike_d, self.v_threshold) / self.k

            else:
                # hard reset
                self.v = self.jit_hard_reset(self.v / self.k, spike_d, self.v_reset)

        else:

            if self.v_reset is None:
                # soft reset
                self.v = self.jit_soft_reset(self.v, spike_d, self.v_threshold)

            else:
                # hard reset
                self.v = self.jit_hard_reset(self.v, spike_d, self.v_reset)


class PSN(nn.Module, base.MultiStepModule):
    def __init__(self, T: int, surrogate_function: surrogate.SurrogateFunctionBase = surrogate.ATan()):
        """
        :param T: the number of time-steps
        :type T: int
        :param surrogate_function: the function for calculating surrogate gradients of the heaviside step function in backward
        :type surrogate_function: Callable

        The Parallel Spiking Neuron proposed in `Parallel Spiking Neurons with High Efficiency and Long-term Dependencies Learning Ability <https://arxiv.org/abs/2304.12760>`_. The neuronal dynamics are defined as

        .. math::

            H &= WX, ~~~~~~~~~~~~~~~W \\in \\mathbb{R}^{T \\times T}, X \\in \\mathbb{R}^{T \\times N} \\label{eq psn neuronal charge}\\\\
            S &= \\Theta(H - B), ~~~~~B \\in \\mathbb{R}^{T}, S\\in \\{0, 1\\}^{T \\times N}

        where :math:`W` is the learnable weight matrix, and :math:`B` is the learnable threshold.

        .. admonition:: Note
            :class: note

            The PSN only supports the multi-step mode.
        """
        super().__init__()
        self.T = T
        self.surrogate_function = surrogate_function
        weight = torch.zeros([T, T])
        bias = torch.zeros([T, 1])

        self.weight = nn.Parameter(weight)
        self.bias = nn.Parameter(bias)

        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        nn.init.constant_(self.bias, -1.)

    def forward(self, x_seq: torch.Tensor):
        # x_seq.shape = [T, N, *]
        h_seq = torch.addmm(self.bias, self.weight, x_seq.flatten(1))
        spike_seq = self.surrogate_function(h_seq)
        return spike_seq.view(x_seq.shape)

    def extra_repr(self):
        return super().extra_repr() + f'T={self.T}, '


class MaskedPSN(base.MemoryModule):
    @staticmethod
    @torch.jit.script
    def gen_masked_weight(lambda_: torch.Tensor, mask0: torch.Tensor, mask1: torch.Tensor, weight: torch.Tensor):
        return (lambda_ * mask0 + (1. - lambda_) * mask1) * weight

    def masked_weight(self):
        if self.lambda_ >= 1.:
            return self.weight * self.mask0
        else:
            return self.gen_masked_weight(self.lambda_, self.mask0, self.mask1, self.weight)

    def __init__(self, k: int, T: int, lambda_init: float = 0.,
                 surrogate_function: surrogate.SurrogateFunctionBase = surrogate.ATan(), step_mode: str = 's'):
        """
        :param k: the order of the Masked PSN
        :type k: int
        :param T: the number of time-steps
        :type T: int
        :param lambda_init: the initial value of :math:`\\lambda` to adjust the progressive masking process
        :type lambda_init: float
        :param surrogate_function: the function for calculating surrogate gradients of the heaviside step function in backward
        :type surrogate_function: Callable
        :param step_mode: the step mode, which can be `s` (single-step) or `m` (multi-step)
        :type step_mode: str

        The Masked Parallel Spiking Neuron proposed in `Parallel Spiking Neurons with High Efficiency and Long-term Dependencies Learning Ability <https://arxiv.org/abs/2304.12760>`_. The neuronal dynamics are defined as

        .. math::

            H &= (W \\cdot {M}_{k})X, ~~~~~~~~~~~~~~~W \\in \\mathbb{R}^{T \\times T}, {M}_{k} \\in \\mathbb{R}^{T \\times T}, X \\in \\mathbb{R}^{T \\times N} \\\\
            S &= \\Theta(H - B), ~~~~~B \\in \\mathbb{R}^{T}, S\\in \\{0, 1\\}^{T \\times N}

        where :math:`W` is the learnable weight matrix, :math:`B` is the learnable threshold, and :math:`{M}_{k}` is defined as

        .. math::

            {M}_{k}[i][j] = \\begin{cases}
                1, ~~ j \\leq i \\leq j + k - 1 \\\\
                0, \\mathrm{otherwise}
            \\end{cases}.

        :math:`\\lambda` is used to adjust the progressive masking process, which is

        .. math::

            M_{k}(\\lambda) = \\lambda \\cdot M_{k} + (1 - \\lambda) \\cdot J,

        where :math:`J` is an all-one matrix.

        The user can set :math:`\\lambda` during training by calling ``self.lambda_ = ...``.

        .. admonition:: Note
            :class: note

            The masked PSN supports both single-step and multi-step mode. But using the multi-step mode is much faster than the single-step mode.

        """
        super().__init__()
        self.register_memory('time_step', 0)
        self.register_memory('queue', [])
        self.step_mode = step_mode
        self.k = k
        self.T = T
        self.surrogate_function = surrogate_function
        weight = torch.zeros([T, T])
        bias = torch.zeros([T, 1])
        self.register_buffer('_lambda_', torch.as_tensor(lambda_init))

        self.weight = nn.Parameter(weight)
        self.bias = nn.Parameter(bias)

        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        nn.init.constant_(self.bias, -1.)

        mask1 = torch.ones([T, T])
        mask0 = torch.tril(mask1) * torch.triu(mask1, -(self.k - 1))
        self.register_buffer('mask0', mask0)
        self.register_buffer('mask1', mask1)


    def single_step_forward(self, x: torch.Tensor):
        if self.lambda_ < 1.:
            raise ValueError("The masked PSN can not work in single-step mode when k < 1!")

        self.queue.append(x.flatten())
        if self.queue.__len__() > self.k:
            self.queue.pop(0)

        if self.time_step + 1 > self.T:
            raise OverflowError(f"The MaskedPSN(T={self.T}) has run {self.time_step + 1} time-steps!")


        weight = self.masked_weight()[self.time_step, self.time_step + 1 - self.queue.__len__(): self.time_step + 1]
        x_seq = torch.stack(self.queue)



        for i in range(x.dim()):
            weight = weight.unsqueeze(-1)


        h = torch.sum(weight * x_seq, 0)
        spike = self.surrogate_function(h + self.bias[self.time_step])

        self.time_step += 1
        return spike.view(x.shape)

    def multi_step_forward(self, x_seq: torch.Tensor):
        # x_seq.shape = [T, N, *]
        assert x_seq.shape[0] == self.T
        h_seq = torch.addmm(self.bias, self.masked_weight(), x_seq.flatten(1))
        spike_seq = self.surrogate_function(h_seq).view(x_seq.shape)
        return spike_seq

    @property
    def lambda_(self):
        return self._lambda_

    @lambda_.setter
    def lambda_(self, value: float):
        torch.fill_(self.lambda_, value)

    def extra_repr(self):
        return super().extra_repr() + f', lambda_={self.lambda_}, T={self.T}'


class SlidingPSN(base.MemoryModule):

    @property
    def supported_backends(self):
        return 'gemm', 'conv'

    def gen_gemm_weight(self, T: int):
        weight = torch.zeros([T, T], device=self.weight.device)
        for i in range(T):
            end = i + 1
            start = max(0, i + 1 - self.k)
            length = min(end - start, self.k)
            weight[i][start: end] = self.weight[self.k - length: self.k]

        return weight

    def __init__(self, k: int, exp_init: bool = True,
                 surrogate_function: surrogate.SurrogateFunctionBase = surrogate.ATan(), step_mode: str = 's',
                 backend: str = 'gemm'):
        """
        :param k: the order of the Sliding PSN
        :type k: int
        :param exp_init: if ``True``, the weight will be initialized as ``(..., 1/4, 1/2, 1)``. If ``False``, the weight    will be initialized by the kaiming uniform
        :type exp_init: bool
        :param surrogate_function: the function for calculating surrogate gradients of the heaviside step function in backward
        :type surrogate_function: Callable
        :param step_mode: the step mode, which can be `s` (single-step) or `m` (multi-step)
        :type step_mode: str
        :param backend: backend fot this neuron layer, which can be "gemm" or "conv". This option only works for the multi-step mode
        :type backend: str

        The Sliding Parallel Spiking Neuron proposed in `Parallel Spiking Neurons with High Efficiency and Long-term Dependencies Learning Ability <https://arxiv.org/abs/2304.12760>`_. The neuronal dynamics are defined as

        .. math::

            H[t] &= \\sum_{i=0}^{k-1}W_{i}\\cdot X[t - k + 1 + i], \\\\
	        S[t] &= \\Theta(H[t] - B),


        where :math:`W = [W_{0}, W_{1}, ..., W_{k-1}] \\in \\mathbb{R}^{T}` is the learnable weight, and :math:`B` is the learnable threshold.


        .. admonition:: Note
            :class: note

            The Sliding PSN supports both single-step and multi-step mode. But using the multi-step mode is much faster than the single-step mode.


        """

        super().__init__()
        self.register_memory('queue', [])
        self.step_mode = step_mode
        self.k = k
        self.surrogate_function = surrogate_function
        self.backend = backend

        if exp_init:
            weight = torch.ones([k])
            for i in range(k - 2, -1, -1):
                weight[i] = weight[i + 1] / 2.
        else:
            weight = torch.ones([1, k])
            nn.init.kaiming_uniform_(weight, a=math.sqrt(5))
            weight = weight[0]

        self.weight = nn.Parameter(weight)
        self.bias = nn.Parameter(torch.as_tensor(-1.))

    def single_step_forward(self, x: torch.Tensor):
        self.queue.append(x.flatten())
        if self.queue.__len__() > self.k:
            self.queue.pop(0)

        weight = self.weight[self.k - self.queue.__len__(): self.k]
        x_seq = torch.stack(self.queue)

        weight = weight.unsqueeze(-1)

        h = torch.sum(weight * x_seq, 0)
        spike = self.surrogate_function(h + self.bias)

        return spike.view(x.shape)

    def multi_step_forward(self, x_seq: torch.Tensor):
        if self.backend == 'gemm':

            weight = self.gen_gemm_weight(x_seq.shape[0])
            h_seq = torch.addmm(self.bias, weight, x_seq.flatten(1)).view(x_seq.shape)
            return self.surrogate_function(h_seq)
        elif self.backend == 'conv':

            # x_seq.shape = [T, N, *]
            x_seq_shape = x_seq.shape
            # [T, N, *] -> [T, N] -> [N, T] -> [N, 1, T]
            x_seq = x_seq.flatten(1).t().unsqueeze(1)

            x_seq = F.pad(x_seq, pad=(self.k - 1, 0))
            x_seq = F.conv1d(x_seq, self.weight.view(1, 1, -1), stride=1)

            x_seq = x_seq.squeeze(1).t().view(x_seq_shape)
            return self.surrogate_function(x_seq + self.bias)

        else:
            raise NotImplementedError(self.backend)

    def extra_repr(self):
        return super().extra_repr() + f', order={self.k}'

class GatedLIFNode(base.MemoryModule):
    def __init__(self, T: int, inplane = None,
                 init_linear_decay = None, init_v_subreset = None, init_tau: float = 0.25, init_v_threshold: float = 0.5, init_conduct: float = 0.5,
                 surrogate_function: Callable = surrogate.Sigmoid(), step_mode='m', backend='torch'):
        """
        * :ref:`中文API <GatedLIFNode.__init__-cn>`

        .. _GatedLIFNode.__init__-cn:

        :param T: 时间步长
        :type T: int

        :param inplane: 输入tensor的通道数。不设置inplane，则默认使用layer-wise GLIF
        :type inplane: int

        :param init_linear_decay: 膜电位线性衰减常数初始值，不设置就默认为init_v_threshold/(T * 2)
        :type init_linear_decay: float

        :param init_v_subreset: 膜电位复位电压初始值
        :type init_v_subreset: float

        :param init_tau: 膜电位时间常数的初始值
        :type init_tau: float

        :param init_v_threshold: 神经元的阈值电压初始值
        :type init_v_threshold: float

        :param init_conduct: 膜电位电导率初始值
        :type init_conduct: float

        :param surrogate_function: 反向传播时用来计算脉冲函数梯度的替代函数
        :type surrogate_function: Callable

        :param step_mode: 步进模式，只支持 `'m'` (多步)
        :type step_mode: str

        :param backend: 使用哪种后端。不同的 ``step_mode`` 可能会带有不同的后端。可以通过打印 ``self.supported_backends`` 查看当前
            使用的步进模式支持的后端。在支持的情况下，使用 ``'cupy'`` 后端是速度最快的。gated-LIF只支持torch
        :type backend: str


        模型出处：`GLIF: A Unified Gated Leaky Integrate-and-Fire Neuron for Spiking Neural Networks <https://openreview.net/forum?id=UmFSx2c4ubT>`
        GLIF中所有的膜电位参数都是可学的，包括新引入的门控系数。

        * :ref:`API in English <GatedLIFNode.__init__-en>`

        .. _GatedLIFNode.__init__-en:

        :param T: time-step
        :type T: int

        :param inplane: input tensor channel number, default: None(layer-wise GLIF). If set, otherwise(channel-wise GLIF)
        :type inplane: int

        :param init_linear_decay: initial linear-decay constant，default: init_v_threshold/(T * 2)
        :type init_linear_decay: float

        :param init_v_subreset: initial soft-reset constant
        :type init_v_subreset: float

        :param init_tau: initial exponential-decay constant
        :type init_tau: float

        :param init_v_threshold: initial menbrane potential threshold
        :type init_v_threshold: float

        :param init_conduct: initial conduct
        :type init_conduct: float

        :param surrogate_function: surrogate gradient
        :type surrogate_function: Callable

        :param step_mode: step mode, only support `'m'` (multi-step)
        :type step_mode: str

        :param backend: backend fot this neuron layer, which can be "gemm" or "conv". This option only works for the multi-step mode
        :type backend: str


        Gated LIF neuron refers to `GLIF: A Unified Gated Leaky Integrate-and-Fire Neuron for Spiking Neural Networks <https://openreview.net/forum?id=UmFSx2c4ubT>`
        All membrane-related parameters are learnable, including the gates.
        """

        assert isinstance(init_tau, float) and init_tau < 1.
        assert isinstance(T, int) and T is not None
        assert isinstance(inplane, int) or inplane is None
        assert (isinstance(init_linear_decay, float) and init_linear_decay < 1.) or init_linear_decay is None
        assert (isinstance(init_v_subreset, float) and init_v_subreset < 1.) or init_v_subreset is None

        assert step_mode == 'm'
        super().__init__()
        self.surrogate_function = surrogate_function
        self.backend = backend
        self.step_mode = step_mode
        self.T = T
        self.register_memory('v', 0.)
        self.register_memory('u', 0.)
        self.channel_wise = inplane is not None
        if self.channel_wise: #channel-wise learnable params
            self.alpha, self.beta, self.gamma = [nn.Parameter(torch.tensor(0.2 * (np.random.rand(inplane) - 0.5), dtype=torch.float)) for i in range(3)]
            self.tau = nn.Parameter(- math.log(1 / init_tau - 1) * torch.ones(inplane, dtype=torch.float))
            self.v_threshold = nn.Parameter(- math.log(1 / init_v_threshold - 1) * torch.ones(inplane, dtype=torch.float))
            init_linear_decay = init_v_threshold / (T * 2) if init_linear_decay is None else init_linear_decay
            self.linear_decay = nn.Parameter(- math.log(1 / init_linear_decay - 1) * torch.ones(inplane, dtype=torch.float))
            init_v_subreset = init_v_threshold if init_v_subreset is None else init_v_subreset
            self.v_subreset = nn.Parameter(- math.log(1 / init_v_subreset - 1) * torch.ones(inplane, dtype=torch.float))
            self.conduct = nn.Parameter(- math.log(1 / init_conduct - 1) * torch.ones((T, inplane), dtype=torch.float))

        else:   #layer-wise learnable params
            self.alpha, self.beta, self.gamma = [nn.Parameter(torch.tensor(0.2 * (np.random.rand() - 0.5), dtype=torch.float)) for i in range(3)]
            self.tau = nn.Parameter(torch.tensor(- math.log(1 / init_tau - 1), dtype=torch.float))
            self.v_threshold = nn.Parameter(torch.tensor(- math.log(1 / init_v_threshold - 1), dtype=torch.float))
            init_linear_decay = init_v_threshold / (T * 2) if init_linear_decay is None else init_linear_decay
            self.linear_decay = nn.Parameter(torch.tensor(- math.log(1 / init_linear_decay - 1), dtype=torch.float))
            init_v_subreset = init_v_threshold if init_v_subreset is None else init_v_subreset
            self.v_subreset = nn.Parameter(torch.tensor(- math.log(1 / init_v_subreset - 1), dtype=torch.float))
            self.conduct = nn.Parameter(- math.log(1 / init_conduct - 1) * torch.ones(T, dtype=torch.float))

    @property
    def supported_backends(self):
        return ('torch',)

    def extra_repr(self):
        with torch.no_grad():
            tau = self.tau
            v_subreset = self.v_subreset
            linear_decay = self.linear_decay
            conduct = self.conduct
        return super().extra_repr() + f', tau={tau}' + f', v_subreset={v_subreset}' + f', linear_decay={linear_decay}' + f', conduct={conduct}'

    def neuronal_charge(self, x: torch.Tensor, alpha: torch.Tensor, beta: torch.Tensor, t):
        input = x * (1 - beta * (1 - self.conduct[t].view(1, -1, 1, 1).sigmoid()))
        self.u = ((1 - alpha * (1 - self.tau.view(1, -1, 1, 1).sigmoid())) * self.v \
                  - (1 - alpha) * self.linear_decay.view(1, -1, 1, 1).sigmoid()) \
                 + input

    def neuronal_reset(self, spike, alpha: torch.Tensor, gamma: torch.Tensor):
        self.u = self.u - (1 - alpha * (1 - self.tau.view(1, -1, 1, 1).sigmoid())) * self.v * gamma * spike \
                 - (1 - gamma) * self.v_subreset.view(1, -1, 1, 1).sigmoid() * spike

    def neuronal_fire(self):
        return self.surrogate_function(self.u - self.v_threshold.view(1, -1, 1, 1).sigmoid())

    def multi_step_forward(self, x_seq: torch.Tensor):
        alpha, beta, gamma = self.alpha.view(1, -1, 1, 1).sigmoid(), self.beta.view(1, -1, 1, 1).sigmoid(), self.gamma.view(1, -1, 1, 1).sigmoid()
        y_seq = []
        spike = torch.zeros(x_seq.shape[1:], device=x_seq.device)
        for t in range(self.T):
            self.neuronal_charge(x_seq[t], alpha, beta, t)
            self.neuronal_reset(spike, alpha, gamma)
            spike = self.neuronal_fire()
            self.v = self.u
            y_seq.append(spike)
        return torch.stack(y_seq)


##########################################################################################################
# DSR modules
##########################################################################################################

import torch.distributed as dist

class DSRIFNode(base.MemoryModule):
    def __init__(self, T: int = 20, v_threshold: float = 6., alpha: float = 0.5, v_threshold_training: bool = True,
                 v_threshold_grad_scaling: float = 1.0, v_threshold_lower_bound: float = 0.01, step_mode='m',
                 backend='torch', **kwargs):

        """
        * :ref:`中文API <DSRIFNode.__init__-cn>`

        .. _DSRIFNode.__init__-cn:

        :param T: 时间步长
        :type T: int

        :param v_threshold: 神经元的阈值电压初始值
        :type v_threshold: float

        :param alpha: 放电阈值的缩放因子
        :type alpha: float

        :param v_threshold_training: 是否将阈值电压设置为可学习参数，默认为`'True'`
        :type v_threshold_training: bool

        :param v_threshold_grad_scaling: 对放电阈值的梯度进行缩放的缩放因子
        :type v_threshold_grad_scaling: float

        :param v_threshold_lower_bound: 训练过程中，阈值电压能取到的最小值
        :type v_threshold_lower_bound: float

        :param step_mode: 步进模式，只支持 `'m'` (多步)
        :type step_mode: str

        :param backend: 使用哪种后端。不同的 ``step_mode`` 可能会带有不同的后端。可以通过打印 ``self.supported_backends`` 查看当前
            使用的步进模式支持的后端。在支持的情况下，使用 ``'cupy'`` 后端是速度最快的。DSR-IF只支持torch
        :type backend: str

        模型出处：`Training High-Performance Low-Latency Spiking Neural Networks by Differentiation on Spike Representation
         <https://arxiv.org/pdf/2205.00459.pdf>`.


        * :ref:`API in English <DSRIFNode.__init__-en>`

        .. _DSRIFNode.__init__-en:

        :param T: time-step
        :type T: int

        :param v_threshold: initial menbrane potential threshold
        :type v_threshold: float

        :param alpha: the scaling factor for the menbrane potential threshold
        :type alpha: float

        :param v_threshold_training: whether the menbrane potential threshold is trained, default: `'True'`
        :type v_threshold_training: bool

        :param v_threshold_grad_scaling: the scaling factor for the gradient of the menbrane potential threshold
        :type v_threshold_grad_scaling: float

        :param v_threshold_lower_bound: the minimum of the menbrane potential threshold during training
        :type v_threshold_lower_bound: float

        :param step_mode: step mode, only support `'m'` (multi-step)
        :type step_mode: str

        :param backend: backend fot this neuron layer, which can be "gemm" or "conv". This option only works for the multi-step mode
        :type backend: str


        DSR IF neuron refers to `Training High-Performance Low-Latency Spiking Neural Networks by Differentiation on Spike Representation
         <https://arxiv.org/pdf/2205.00459.pdf>`.
        """

        assert isinstance(T, int) and T is not None
        assert isinstance(v_threshold, float) and v_threshold >= v_threshold_lower_bound
        assert isinstance(alpha, float) and alpha > 0.0 and alpha <= 1.0
        assert isinstance(v_threshold_lower_bound, float) and v_threshold_lower_bound > 0.0
        assert step_mode == 'm'

        super().__init__()
        self.backend = backend
        self.step_mode = step_mode
        self.T = T
        if v_threshold_training:
            self.v_threshold = nn.Parameter(torch.tensor(v_threshold))
        else:
            self.v_threshold = torch.tensor(v_threshold)
        self.alpha = alpha
        self.v_threshold_lower_bound = v_threshold_lower_bound
        self.v_threshold_grad_scaling = v_threshold_grad_scaling

    @property
    def supported_backends(self):
        return ('torch',)

    def extra_repr(self):
        with torch.no_grad():
            T = self.T
            v_threshold = self.v_threshold
            alpha = self.alpha
            v_threshold_lower_bound = self.v_threshold_lower_bound
            v_threshold_grad_scaling = self.v_threshold_grad_scaling
        return f', T={T}' + f', init_vth={v_threshold}' + f', alpha={alpha}' + f', vth_bound={v_threshold_lower_bound}' + f', vth_g_scale={v_threshold_grad_scaling}'

    def multi_step_forward(self, x_seq: torch.Tensor):
        with torch.no_grad():
            self.v_threshold.copy_(
                F.relu(self.v_threshold - self.v_threshold_lower_bound) + self.v_threshold_lower_bound)
        iffunc = self.DSRIFFunction.apply
        y_seq = iffunc(x_seq, self.T, self.v_threshold, self.alpha, self.v_threshold_grad_scaling)
        return y_seq


    class DSRIFFunction(torch.autograd.Function):
        @staticmethod
        def forward(ctx, inp, T=10, v_threshold=1.0, alpha=0.5, v_threshold_grad_scaling=1.0):
            ctx.save_for_backward(inp)

            mem_potential = torch.zeros_like(inp[0]).to(inp.device)
            spikes = []

            for t in range(inp.size(0)):
                mem_potential = mem_potential + inp[t]
                spike = ((mem_potential >= alpha * v_threshold).float() * v_threshold).float()
                mem_potential = mem_potential - spike
                spikes.append(spike)
            output = torch.stack(spikes)

            ctx.T = T
            ctx.v_threshold = v_threshold
            ctx.v_threshold_grad_scaling = v_threshold_grad_scaling
            return output

        @staticmethod
        def backward(ctx, grad_output):
            with torch.no_grad():
                inp = ctx.saved_tensors[0]
                T = ctx.T
                v_threshold = ctx.v_threshold
                v_threshold_grad_scaling = ctx.v_threshold_grad_scaling

                input_rate_coding = torch.mean(inp, 0)
                grad_output_coding = torch.mean(grad_output, 0) * T

                input_grad = grad_output_coding.clone()
                input_grad[(input_rate_coding < 0) | (input_rate_coding > v_threshold)] = 0
                input_grad = torch.stack([input_grad for _ in range(T)]) / T

                v_threshold_grad = grad_output_coding.clone()
                v_threshold_grad[input_rate_coding <= v_threshold] = 0
                v_threshold_grad = torch.sum(v_threshold_grad) * v_threshold_grad_scaling
                if v_threshold_grad.is_cuda and torch.cuda.device_count() != 1:
                    try:
                        dist.all_reduce(v_threshold_grad, op=dist.ReduceOp.SUM)
                    except:
                        raise RuntimeWarning(
                            'Something wrong with the `all_reduce` operation when summing up the gradient of v_threshold from multiple gpus. Better check the gpu status and try DistributedDataParallel.')

                return input_grad, None, v_threshold_grad, None, None


class DSRLIFNode(base.MemoryModule):
    def __init__(self, T: int = 20, v_threshold: float = 1., tau: float = 2.0, delta_t: float = 0.05,
                 alpha: float = 0.3, v_threshold_training: bool = True,
                 v_threshold_grad_scaling: float = 1.0, v_threshold_lower_bound: float = 0.1, step_mode='m',
                 backend='torch', **kwargs):

        """
        * :ref:`中文API <DSRLIFNode.__init__-cn>`

        .. _DSRLIFNode.__init__-cn:

        :param T: 时间步长
        :type T: int

        :param v_threshold: 神经元的阈值电压初始值
        :type v_threshold: float

        :param tau: 膜电位时间常数
        :type tau: float

        :param delta_t: 对微分方程形式的LIF模型进行离散化的步长
        :type delta_t: float

        :param alpha: 放电阈值的缩放因子
        :type alpha: float

        :param v_threshold_training: 是否将阈值电压设置为可学习参数，默认为`'True'`
        :type v_threshold_training: bool

        :param v_threshold_grad_scaling: 对放电阈值的梯度进行缩放的缩放因子
        :type v_threshold_grad_scaling: float

        :param v_threshold_lower_bound: 训练过程中，阈值电压能取到的最小值
        :type v_threshold_lower_bound: float

        :param step_mode: 步进模式，只支持 `'m'` (多步)
        :type step_mode: str

        :param backend: 使用哪种后端。不同的 ``step_mode`` 可能会带有不同的后端。可以通过打印 ``self.supported_backends`` 查看当前
            使用的步进模式支持的后端。在支持的情况下，使用 ``'cupy'`` 后端是速度最快的。DSR-IF只支持torch
        :type backend: str

        模型出处：`Training High-Performance Low-Latency Spiking Neural Networks by Differentiation on Spike Representation
         <https://arxiv.org/pdf/2205.00459.pdf>`.


        * :ref:`API in English <DSRLIFNode.__init__-en>`

        .. _DSRLIFNode.__init__-en:

        :param T: time-step
        :type T: int

        :param v_threshold: initial menbrane potential threshold
        :type v_threshold: float

        :param tau: membrane time constant
        :type tau: float

        :param delta_t: discretization step for discretizing the ODE version of the LIF model
        :type delta_t: float

        :param alpha: the scaling factor for the menbrane potential threshold
        :type alpha: float

        :param v_threshold_training: whether the menbrane potential threshold is trained, default: `'True'`
        :type v_threshold_training: bool

        :param v_threshold_grad_scaling: the scaling factor for the gradient of the menbrane potential threshold
        :type v_threshold_grad_scaling: float

        :param v_threshold_lower_bound: the minimum of the menbrane potential threshold during training
        :type v_threshold_lower_bound: float

        :param step_mode: step mode, only support `'m'` (multi-step)
        :type step_mode: str

        :param backend: backend fot this neuron layer, which can be "gemm" or "conv". This option only works for the multi-step mode
        :type backend: str


        DSR LIF neuron refers to `Training High-Performance Low-Latency Spiking Neural Networks by Differentiation on Spike Representation
         <https://arxiv.org/pdf/2205.00459.pdf>`.
        """

        assert isinstance(T, int) and T is not None
        assert isinstance(v_threshold, float) and v_threshold >= v_threshold_lower_bound
        assert isinstance(alpha, float) and alpha > 0.0 and alpha <= 1.0
        assert isinstance(v_threshold_lower_bound, float) and v_threshold_lower_bound > 0.0
        assert step_mode == 'm'

        super().__init__()
        self.backend = backend
        self.step_mode = step_mode
        self.T = T
        if v_threshold_training:
            self.v_threshold = nn.Parameter(torch.tensor(v_threshold))
        else:
            self.v_threshold = torch.tensor(v_threshold)
        self.tau = tau
        self.delta_t = delta_t
        self.alpha = alpha
        self.v_threshold_lower_bound = v_threshold_lower_bound
        self.v_threshold_grad_scaling = v_threshold_grad_scaling

    @property
    def supported_backends(self):
        return ('torch',)

    def extra_repr(self):
        with torch.no_grad():
            T = self.T
            v_threshold = self.v_threshold
            tau = self.tau
            delta_t = self.delta_t
            alpha = self.alpha
            v_threshold_lower_bound = self.v_threshold_lower_bound
            v_threshold_grad_scaling = self.v_threshold_grad_scaling
        return f', T={T}' + f', init_vth={v_threshold}' + f', tau={tau}' + f', dt={delta_t}' + f', alpha={alpha}' + \
               f', vth_bound={v_threshold_lower_bound}' + f', vth_g_scale={v_threshold_grad_scaling}'

    def multi_step_forward(self, x_seq: torch.Tensor):
        with torch.no_grad():
            self.v_threshold.copy_(
                F.relu(self.v_threshold - self.v_threshold_lower_bound) + self.v_threshold_lower_bound)
        liffunc = self.DSRLIFFunction.apply
        y_seq = liffunc(x_seq, self.T, self.v_threshold, self.tau, self.delta_t, self.alpha,
                        self.v_threshold_grad_scaling)
        return y_seq

    @classmethod
    def weight_rate_spikes(cls, data, tau, delta_t):
        T = data.shape[0]
        chw = data.size()[2:]
        data_reshape = data.permute(list(range(1, len(chw) + 2)) + [0])
        weight = torch.tensor([math.exp(-1 / tau * (delta_t * T - ii * delta_t)) for ii in range(1, T + 1)]).to(
            data_reshape.device)
        return (weight * data_reshape).sum(dim=len(chw) + 1) / weight.sum()

    class DSRLIFFunction(torch.autograd.Function):
        @staticmethod
        def forward(ctx, inp, T, v_threshold, tau, delta_t=0.05, alpha=0.3, v_threshold_grad_scaling=1.0):
            ctx.save_for_backward(inp)

            mem_potential = torch.zeros_like(inp[0]).to(inp.device)
            beta = math.exp(-delta_t / tau)

            spikes = []
            for t in range(inp.size(0)):
                mem_potential = beta * mem_potential + (1 - beta) * inp[t]
                spike = ((mem_potential >= alpha * v_threshold).float() * v_threshold).float()
                mem_potential = mem_potential - spike
                spikes.append(spike / delta_t)
            output = torch.stack(spikes)

            ctx.T = T
            ctx.v_threshold = v_threshold
            ctx.tau = tau
            ctx.delta_t = delta_t
            ctx.v_threshold_grad_scaling = v_threshold_grad_scaling
            return output

        @staticmethod
        def backward(ctx, grad_output):
            inp = ctx.saved_tensors[0]
            T = ctx.T
            v_threshold = ctx.v_threshold
            delta_t = ctx.delta_t
            tau = ctx.tau
            v_threshold_grad_scaling = ctx.v_threshold_grad_scaling

            input_rate_coding = DSRLIFNode.weight_rate_spikes(inp, tau, delta_t)
            grad_output_coding = DSRLIFNode.weight_rate_spikes(grad_output, tau, delta_t) * T

            indexes = (input_rate_coding > 0) & (input_rate_coding < v_threshold / delta_t * tau)
            input_grad = torch.zeros_like(grad_output_coding)
            input_grad[indexes] = grad_output_coding[indexes].clone() / tau
            input_grad = torch.stack([input_grad for _ in range(T)]) / T

            v_threshold_grad = grad_output_coding.clone()
            v_threshold_grad[input_rate_coding <= v_threshold / delta_t * tau] = 0
            v_threshold_grad = torch.sum(v_threshold_grad) * delta_t * v_threshold_grad_scaling
            if v_threshold_grad.is_cuda and torch.cuda.device_count() != 1:
                try:
                    dist.all_reduce(v_threshold_grad, op=dist.ReduceOp.SUM)
                except:
                    raise RuntimeWarning('Something wrong with the `all_reduce` operation when summing up the gradient of v_threshold from multiple gpus. Better check the gpu status and try DistributedDataParallel.')

            return input_grad, None, v_threshold_grad, None, None, None, None


##########################################################################################################
# OTTT modules
##########################################################################################################

class OTTTLIFNode(LIFNode):
    def __init__(self, tau: float = 2., decay_input: bool = False, v_threshold: float = 1.,
                 v_reset: Optional[float] = None, surrogate_function: Callable = surrogate.Sigmoid(),
                 detach_reset: bool = True, step_mode='s', backend='torch', store_v_seq: bool = False):
        """
        * :ref:`API in English <OTTTLIFNode.__init__-en>`

        .. _OTTTLIFNode.__init__-cn:

        :param tau: 膜电位时间常数
        :type tau: float

        :param decay_input: 输入是否也会参与衰减
        :type decay_input: bool

        :param v_threshold: 神经元的阈值电压
        :type v_threshold: float

        :param v_reset: 神经元的重置电压。如果不为 ``None``，当神经元释放脉冲后，电压会被重置为 ``v_reset``；
            如果设置为 ``None``，当神经元释放脉冲后，电压会被减去 ``v_threshold``
        :type v_reset: Optional[float]

        :param surrogate_function: 反向传播时用来计算脉冲函数梯度的替代函数
        :type surrogate_function: Callable

        :param detach_reset: 是否将reset过程的计算图分离。该参数在本模块中不起作用，仅为保持代码统一而保留
        :type detach_reset: bool

        :param step_mode: 步进模式，为了保证神经元的显存占用小，仅可以为 `'s'` (单步)
        :type step_mode: str

        :param backend: 使用哪种后端。不同的 ``step_mode`` 可能会带有不同的后端。可以通过打印 ``self.supported_backends`` 查看当前
            使用的步进模式支持的后端。在支持的情况下，使用 ``'cupy'`` 后端是速度最快的
        :type backend: str

        :param store_v_seq: 在使用 ``step_mode = 'm'`` 时，给与 ``shape = [T, N, *]`` 的输入后，是否保存中间过程的 ``shape = [T, N, *]``
            的各个时间步的电压值 ``self.v_seq`` 。设置为 ``False`` 时计算完成后只保留最后一个时刻的电压，即 ``shape = [N, *]`` 的 ``self.v`` 。
            通常设置成 ``False`` ，可以节省内存
        :type store_v_seq: bool

        神经元模型出处：`Online Training Through Time for Spiking Neural Networks <https://arxiv.org/pdf/2210.04195.pdf>`
        模型正向传播和Leaky Integrate-and-Fire神经元相同；用于随时间在线训练


        * :ref:`中文API <OTTTLIFNode.__init__-cn>`

        .. _OTTTLIFNode.__init__-en:

        :param tau: membrane time constant
        :type tau: float

        :param decay_input: whether the input will decay
        :type decay_input: bool

        :param v_threshold: threshold of this neurons layer
        :type v_threshold: float

        :param v_reset: reset voltage of this neurons layer. If not ``None``, the neuron's voltage will be set to ``v_reset``
            after firing a spike. If ``None``, the neuron's voltage will subtract ``v_threshold`` after firing a spike
        :type v_reset: Optional[float]

        :param surrogate_function: the function for calculating surrogate gradients of the heaviside step function in backward
        :type surrogate_function: Callable

        :param detach_reset: whether detach the computation graph of reset in backward. this parameter does not take any effect in
            the module, and is retained solely for code consistency
        :type detach_reset: bool

        :param step_mode: the step mode, which can solely be `s` (single-step) to guarantee the memory-efficient computation
        :type step_mode: str

        :param backend: backend fot this neurons layer. Different ``step_mode`` may support for different backends. The user can
        print ``self.supported_backends`` and check what backends are supported by the current ``step_mode``. If supported,
        using ``'cupy'`` backend will have the fastest training speed
        :type backend: str

        :param store_v_seq: when using ``step_mode = 'm'`` and given input with ``shape = [T, N, *]``, this option controls
            whether storing the voltage at each time-step to ``self.v_seq`` with ``shape = [T, N, *]``. If set to ``False``,
            only the voltage at last time-step will be stored to ``self.v`` with ``shape = [N, *]``, which can reduce the
            memory consumption
        :type store_v_seq: bool

        OTTT LIF neuron refers to `Online Training Through Time for Spiking Neural Networks <https://arxiv.org/pdf/2210.04195.pdf>`
        The forward propagation is the same as the Leaky Integrate-and-Fire neuron; used for online training through time

        """

        super().__init__(tau, decay_input, v_threshold, v_reset, surrogate_function, detach_reset, step_mode, backend, store_v_seq)
        assert step_mode == 's', "Please use single-step mode to enable memory-efficient training."
        """
        膜电位将在前向传播过程中重新登记为缓存，以支持多卡分布式训练的情况下保留信息在各时刻进行多次反向传播

        membrane potential will be registered as buffer during forward, to support multiple backpropagation for all time steps with 
        reserved informtion under distributed training on multiple GPUs
        """
        self._memories.pop('v')

    def reset(self):
        super().reset()
        if hasattr(self, 'v'):
            del self.v
        if hasattr(self, 'trace'):
            del self.trace

    @property
    def supported_backends(self):
        if self.step_mode == 's':
            return ('torch',)
        else:
            raise ValueError(self.step_mode)

    def neuronal_charge(self, x: torch.Tensor):
        self.v = self.v.detach()

        if self.decay_input:
            if self.v_reset is None or self.v_reset == 0.:
                self.v = self.neuronal_charge_decay_input_reset0(x, self.v, self.tau)
            else:
                self.v = self.neuronal_charge_decay_input(x, self.v, self.v_reset, self.tau)

        else:
            if self.v_reset is None or self.v_reset == 0.:
                self.v = self.neuronal_charge_no_decay_input_reset0(x, self.v, self.tau)
            else:
                self.v = self.neuronal_charge_no_decay_input(x, self.v, self.v_reset, self.tau)

    @staticmethod
    @torch.jit.script
    def track_trace(spike: torch.Tensor, trace: torch.Tensor, tau: float):
        with torch.no_grad():
            trace = trace * (1. - 1. / tau) + spike
        return trace


    def single_step_forward(self, x: torch.Tensor):
        """
        训练时，输出脉冲和迹；推理时，输出脉冲
        训练时需要将后续参数模块用layer.py中定义的GradwithTrace进行包装，根据迹计算梯度
        
        output spike and trace during training; output spike during inference
        during training, successive parametric modules shoule be wrapped by GradwithTrace defined in layer.py, to calculate gradients with traces
        """

        if not hasattr(self, 'v'):
            if self.v_reset is None:
                self.register_buffer('v', torch.zeros_like(x))
            else:
                self.register_buffer('v', torch.ones_like(x) * self.v_reset)

        if self.training:
            if not hasattr(self, 'trace'):
                self.register_buffer('trace', torch.zeros_like(x))
    
            if self.backend == 'torch':
                self.neuronal_charge(x)
                spike = self.neuronal_fire()
                self.neuronal_reset(spike)

                self.trace = self.track_trace(spike, self.trace, self.tau)

                return [spike, self.trace]
            else:
                raise ValueError(self.backend)
        else:
            if self.v_reset is None:
                if self.decay_input:
                    spike, self.v = self.jit_eval_single_step_forward_soft_reset_decay_input(x, self.v,
                                                                                             self.v_threshold, self.tau)
                else:
                    spike, self.v = self.jit_eval_single_step_forward_soft_reset_no_decay_input(x, self.v,
                                                                                                self.v_threshold,
                                                                                                self.tau)
            else:
                if self.decay_input:
                    spike, self.v = self.jit_eval_single_step_forward_hard_reset_decay_input(x, self.v,
                                                                                             self.v_threshold,
                                                                                             self.v_reset, self.tau)
                else:
                    spike, self.v = self.jit_eval_single_step_forward_hard_reset_no_decay_input(x, self.v,
                                                                                                self.v_threshold,
                                                                                                self.v_reset,
                                                                                                self.tau)
            return spike


##########################################################################################################
# SLTT modules
##########################################################################################################

class SLTTLIFNode(LIFNode):
    def __init__(self, tau: float = 2., decay_input: bool = True, v_threshold: float = 1.,
                 v_reset: Optional[float] = 0., surrogate_function: Callable = surrogate.Sigmoid(),
                 detach_reset: bool = True, step_mode='s', backend='torch', store_v_seq: bool = False):
        """
        * :ref:`API in English <SLTTLIFNode.__init__-en>`

        .. _SLTTLIFNode.__init__-cn:

        :param tau: 膜电位时间常数
        :type tau: float

        :param decay_input: 输入是否也会参与衰减
        :type decay_input: bool

        :param v_threshold: 神经元的阈值电压
        :type v_threshold: float

        :param v_reset: 神经元的重置电压。如果不为 ``None``，当神经元释放脉冲后，电压会被重置为 ``v_reset``；
            如果设置为 ``None``，当神经元释放脉冲后，电压会被减去 ``v_threshold``
        :type v_reset: Optional[float]

        :param surrogate_function: 反向传播时用来计算脉冲函数梯度的替代函数
        :type surrogate_function: Callable

        :param detach_reset: 是否将reset过程的计算图分离。该参数在本模块中不起作用，仅为保持代码统一而保留
        :type detach_reset: bool

        :param step_mode: 步进模式，为了保证神经元的显存占用小，仅可以为 `'s'` (单步)
        :type step_mode: str

        :param backend: 使用哪种后端。不同的 ``step_mode`` 可能会带有不同的后端。可以通过打印 ``self.supported_backends`` 查看当前
            使用的步进模式支持的后端。在支持的情况下，使用 ``'cupy'`` 后端是速度最快的
        :type backend: str

        :param store_v_seq: 在使用 ``step_mode = 'm'`` 时，给与 ``shape = [T, N, *]`` 的输入后，是否保存中间过程的 ``shape = [T, N, *]``
            的各个时间步的电压值 ``self.v_seq`` 。设置为 ``False`` 时计算完成后只保留最后一个时刻的电压，即 ``shape = [N, *]`` 的 ``self.v`` 。
            通常设置成 ``False`` ，可以节省内存
        :type store_v_seq: bool

        神经元模型出处：`Towards Memory- and Time-Efficient Backpropagation for Training Spiking Neural Networks
        <https://arxiv.org/pdf/2302.14311.pdf>`.模型正向传播和Leaky Integrate-and-Fire神经元相同.


        * :ref:`中文API <SLTTLIFNode.__init__-cn>`

        .. _SLTTLIFNode.__init__-en:

        :param tau: membrane time constant
        :type tau: float

        :param decay_input: whether the input will decay
        :type decay_input: bool

        :param v_threshold: threshold of this neurons layer
        :type v_threshold: float

        :param v_reset: reset voltage of this neurons layer. If not ``None``, the neuron's voltage will be set to ``v_reset``
            after firing a spike. If ``None``, the neuron's voltage will subtract ``v_threshold`` after firing a spike
        :type v_reset: Optional[float]

        :param surrogate_function: the function for calculating surrogate gradients of the heaviside step function in backward
        :type surrogate_function: Callable

        :param detach_reset: whether detach the computation graph of reset in backward. this parameter does not take any effect in
            the module, and is retained solely for code consistency
        :type detach_reset: bool

        :param step_mode: the step mode, which can solely be `s` (single-step) to guarantee the memory-efficient computation
        :type step_mode: str

        :param backend: backend fot this neurons layer. Different ``step_mode`` may support for different backends. The user can
        print ``self.supported_backends`` and check what backends are supported by the current ``step_mode``. If supported,
        using ``'cupy'`` backend will have the fastest training speed
        :type backend: str

        :param store_v_seq: when using ``step_mode = 'm'`` and given input with ``shape = [T, N, *]``, this option controls
            whether storing the voltage at each time-step to ``self.v_seq`` with ``shape = [T, N, *]``. If set to ``False``,
            only the voltage at last time-step will be stored to ``self.v`` with ``shape = [N, *]``, which can reduce the
            memory consumption
        :type store_v_seq: bool

        SLTT LIF neuron refers to `Towards Memory- and Time-Efficient Backpropagation for Training Spiking Neural Networks
        <https://arxiv.org/pdf/2302.14311.pdf>`. The forward propagation is the same as the Leaky Integrate-and-Fire neuron's.

        """

        super().__init__(tau, decay_input, v_threshold, v_reset, surrogate_function, detach_reset, step_mode, backend, store_v_seq)
        assert step_mode == 's', "Please use single-step mode to enable memory-efficient training."
        self._memories.pop('v')

    def reset(self):
        super().reset()
        if hasattr(self, 'v'):
            del self.v

    @property
    def supported_backends(self):
        if self.step_mode == 's':
            return ('torch',)
        else:
            raise ValueError(self.step_mode)

    def neuronal_charge(self, x: torch.Tensor):
        self.v = self.v.detach()

        if self.decay_input:
            if self.v_reset is None or self.v_reset == 0.:
                self.v = self.neuronal_charge_decay_input_reset0(x, self.v, self.tau)
            else:
                self.v = self.neuronal_charge_decay_input(x, self.v, self.v_reset, self.tau)

        else:
            if self.v_reset is None or self.v_reset == 0.:
                self.v = self.neuronal_charge_no_decay_input_reset0(x, self.v, self.tau)
            else:
                self.v = self.neuronal_charge_no_decay_input(x, self.v, self.v_reset, self.tau)

    def single_step_forward(self, x: torch.Tensor):

        if not hasattr(self, 'v'):
            if self.v_reset is None:
                self.register_buffer('v', torch.zeros_like(x))
            else:
                self.register_buffer('v', torch.ones_like(x) * self.v_reset)

        if self.training:
            if self.backend == 'torch':
                self.neuronal_charge(x)
                spike = self.neuronal_fire()
                self.neuronal_reset(spike)
                return spike
            else:
                raise ValueError(self.backend)
        else:
            if self.v_reset is None:
                if self.decay_input:
                    spike, self.v = self.jit_eval_single_step_forward_soft_reset_decay_input(x, self.v,
                                                                                             self.v_threshold, self.tau)
                else:
                    spike, self.v = self.jit_eval_single_step_forward_soft_reset_no_decay_input(x, self.v,
                                                                                                self.v_threshold,
                                                                                                self.tau)
            else:
                if self.decay_input:
                    spike, self.v = self.jit_eval_single_step_forward_hard_reset_decay_input(x, self.v,
                                                                                             self.v_threshold,
                                                                                             self.v_reset, self.tau)
                else:
                    spike, self.v = self.jit_eval_single_step_forward_hard_reset_no_decay_input(x, self.v,
                                                                                                self.v_threshold,
                                                                                                self.v_reset,
                                                                                                self.tau)
            return spike


##########################################################################################################
# Current-based LIF (CLIF) modules
##########################################################################################################

class CLIFNode(BaseNode):
    def __init__(self, c_decay: float = 0.5, v_decay: float = 0.75, v_threshold: float = 0.5,
                 v_reset: float = 0., surrogate_function: Callable = surrogate.Rect()):

        super().__init__(v_threshold, v_reset, surrogate_function)

        self.register_memory('c', 0.)

        self.c_decay = c_decay
        self.v_decay = v_decay

    def neuronal_charge(self, x: torch.Tensor):
        self.c = self.c * self.c_decay + x
        self.v = self.v * self.v_decay + self.c

    def single_step_forward(self, x: torch.Tensor):
        self.v_float_to_tensor(x)
        self.c_float_to_tensor(x)
        self.neuronal_charge(x)
        spike = self.neuronal_fire()
        self.neuronal_reset(spike)
        return spike

    def multi_step_forward(self, x_seq: torch.Tensor):
        T = x_seq.shape[0]
        spike_seq = []

        for t in range(T):
            spike = self.single_step_forward(x_seq[t])
            spike_seq.append(spike)

        return torch.stack(spike_seq)

    def c_float_to_tensor(self, c: torch.Tensor):
        if isinstance(self.c, float):
            c_init = self.c
            self.c = torch.full_like(c.data, fill_value=c_init)


##########################################################################################################
# Noisy modules for exploration of RL
##########################################################################################################

"""Generate colored noise."""

from typing import Union, Iterable, Optional
from numpy import sqrt, newaxis, integer
from numpy.fft import irfft, rfftfreq
from numpy.random import default_rng, Generator, RandomState
from numpy import sum as npsum


def powerlaw_psd_gaussian(
        exponent: float, 
        size: Union[int, Iterable[int]], 
        fmin: float = 0.0, 
        random_state: Optional[Union[int, Generator, RandomState]] = None
    ):
    """Gaussian (1/f)**beta noise.

    Based on the algorithm in:
    Timmer, J. and Koenig, M.:
    On generating power law noise.
    Astron. Astrophys. 300, 707-710 (1995)

    Normalised to unit variance

    Parameters:
    -----------

    exponent : float
        The power-spectrum of the generated noise is proportional to

        S(f) = (1 / f)**beta
        flicker / pink noise:   exponent beta = 1
        brown noise:            exponent beta = 2

        Furthermore, the autocorrelation decays proportional to lag**-gamma
        with gamma = 1 - beta for 0 < beta < 1.
        There may be finite-size issues for beta close to one.

    size : Union[int, Iterable[int]]
        The output has the given shape, and the desired power spectrum in
        the last coordinate. That is, the last dimension is taken as time,
        and all other components are independent.

    fmin : float, optional
        Low-frequency cutoff.
        Default: 0 corresponds to original paper. 
        
        The power-spectrum below fmin is flat. fmin is defined relative
        to a unit sampling rate (see numpy's rfftfreq). For convenience,
        the passed value is mapped to max(fmin, 1/samples) internally
        since 1/samples is the lowest possible finite frequency in the
        sample. The largest possible value is fmin = 0.5, the Nyquist
        frequency. The output for this value is white noise.

    random_state :  int, numpy.integer, numpy.random.Generator, numpy.random.RandomState, 
                    optional
        Optionally sets the state of NumPy's underlying random number generator.
        Integer-compatible values or None are passed to np.random.default_rng.
        np.random.RandomState or np.random.Generator are used directly.
        Default: None.

    Returns
    -------
    out : array
        The samples.


    Examples:
    ---------

    # generate 1/f noise == pink noise == flicker noise
    >>> import colorednoise as cn
    >>> y = cn.powerlaw_psd_gaussian(1, 5)
    """
    
    # Make sure size is a list so we can iterate it and assign to it.
    if isinstance(size, (integer, int)):
        size = [size]
    elif isinstance(size, Iterable):
        size = list(size)
    else:
        raise ValueError("Size must be of type int or Iterable[int]")
    
    # The number of samples in each time series
    samples = size[-1]
    
    # Calculate Frequencies (we asume a sample rate of one)
    # Use fft functions for real output (-> hermitian spectrum)
    f = rfftfreq(samples) # type: ignore # mypy 1.5.1 has problems here 
    
    # Validate / normalise fmin
    if 0 <= fmin <= 0.5:
        fmin = max(fmin, 1./samples) # Low frequency cutoff
    else:
        raise ValueError("fmin must be chosen between 0 and 0.5.")
    
    # Build scaling factors for all frequencies
    s_scale = f    
    ix   = npsum(s_scale < fmin)   # Index of the cutoff
    if ix and ix < len(s_scale):
        s_scale[:ix] = s_scale[ix]
    s_scale = s_scale**(-exponent/2.)
    
    # Calculate theoretical output standard deviation from scaling
    w      = s_scale[1:].copy()
    w[-1] *= (1 + (samples % 2)) / 2. # correct f = +-0.5
    sigma = 2 * sqrt(npsum(w**2)) / samples
    
    # Adjust size to generate one Fourier component per frequency
    size[-1] = len(f)

    # Add empty dimension(s) to broadcast s_scale along last
    # dimension of generated random power + phase (below)
    dims_to_add = len(size) - 1
    s_scale     = s_scale[(newaxis,) * dims_to_add + (Ellipsis,)]
    
    # prepare random number generator
    normal_dist = _get_normal_distribution(random_state)

    # Generate scaled random power + phase
    sr = normal_dist(scale=s_scale, size=size)
    si = normal_dist(scale=s_scale, size=size)
    
    # If the signal length is even, frequencies +/- 0.5 are equal
    # so the coefficient must be real.
    if not (samples % 2):
        si[..., -1] = 0
        sr[..., -1] *= sqrt(2)    # Fix magnitude
    
    # Regardless of signal length, the DC component must be real
    si[..., 0] = 0
    sr[..., 0] *= sqrt(2)    # Fix magnitude
    
    # Combine power + corrected phase to Fourier components
    s  = sr + 1J * si
    
    # Transform to real time series & scale to unit variance
    y = irfft(s, n=samples, axis=-1) / sigma
    
    return y


def _get_normal_distribution(random_state: Optional[Union[int, Generator, RandomState]]):
    normal_dist = None
    if isinstance(random_state, (integer, int)) or random_state is None:
        random_state = default_rng(random_state)
        normal_dist = random_state.normal
    elif isinstance(random_state, (Generator, RandomState)):
        normal_dist = random_state.normal
    else:
        raise ValueError(
            "random_state must be one of integer, numpy.random.Generator, "
            "numpy.random.Randomstate"
        )
    return normal_dist

class NoisyBaseNode(nn.Module, base.MultiStepModule):
    def __init__(self, num_node, is_training: bool = True, T: int = 5, sigma_init: float = 0.5, 
                 beta: float = 0.0, v_threshold: float = 0.5, v_reset: Optional[float] = 0.,
                 surrogate_function: Callable = surrogate.Rect()):
        assert isinstance(v_reset, float) or v_reset is None
        assert isinstance(v_threshold, float)
        super().__init__()

        self.num_node = num_node
        self.is_training = is_training
        self.T = T
        self.beta = beta

        self.sigma_v = sigma_init / math.sqrt(num_node)
        self.cn_v = None

        self.sigma_s = sigma_init / math.sqrt(num_node)
        self.cn_s = None

        self.v_threshold = v_threshold
        self.v_reset = v_reset

        self.surrogate_function = surrogate_function

    @abstractmethod
    def neuronal_charge(self, x: torch.Tensor):
        raise NotImplementedError

    def neuronal_fire(self):
        return self.surrogate_function(self.v - self.v_threshold)

    def neuronal_reset(self, spike):
        if self.v_reset is None:
            self.v = self.v - spike * self.v_threshold
        else:
            self.v = (1. - spike) * self.v + spike * self.v_reset

    def init_tensor(self, data: torch.Tensor):
        self.v = torch.full_like(data, fill_value=self.v_reset)

    def forward(self, x_seq: torch.Tensor):
        self.init_tensor(x_seq[0].data)
        
        y = []

        if self.is_training:
            if self.cn_v is None or self.cn_s is None:
                self.noise_step += 1

            for t in range(self.T):      
                if self.cn_v is None:
                    self.neuronal_charge(x_seq[t] + self.sigma_v * self.eps_v_seq[self.noise_step][t].to(x_seq.device))
                else:
                    self.neuronal_charge(x_seq[t] + self.sigma_v * self.cn_v[:, t])
                spike = self.neuronal_fire()
                self.neuronal_reset(spike)
                if self.cn_s is None:
                    spike = spike + self.sigma_s * self.eps_s_seq[self.noise_step][t].to(x_seq.device)
                else:
                    spike = spike + self.sigma_s * self.cn_s[:, t]
                y.append(spike)
            
        else:
            for t in range(self.T):
                self.neuronal_charge(x_seq[t])
                spike = self.neuronal_fire()
                self.neuronal_reset(spike)
                y.append(spike)

        return torch.stack(y)
        
    def reset_noise(self, num_rl_step):
        eps_shape = [self.num_node, num_rl_step * self.T]
        per_order = [1, 2, 0]
        # (nodes, steps * T) -> (nodes, steps, T) -> (steps, T, nodes)
        self.eps_v_seq = torch.FloatTensor(powerlaw_psd_gaussian(self.beta, eps_shape).reshape(self.num_node, num_rl_step, self.T)).permute(per_order)
        self.eps_s_seq = torch.FloatTensor(powerlaw_psd_gaussian(self.beta, eps_shape).reshape(self.num_node, num_rl_step, self.T)).permute(per_order)
        self.noise_step = -1

    def get_colored_noise(self):
        cn = [self.eps_v_seq[self.noise_step], self.eps_s_seq[self.noise_step]]
        return torch.cat(cn, dim=1)

    def load_colored_noise(self, cn):
        self.cn_v = cn[:, :, :self.num_node]
        self.cn_s = cn[:, :, self.num_node:]

    def cancel_load(self):
        self.cn_v = None
        self.cn_s = None


class NoisyCLIFNode(NoisyBaseNode):
    def __init__(self, num_node, c_decay: float = 0.5, v_decay: float = 0.75, is_training: bool = True, 
                 T: int = 5, sigma_init: float = 0.5, beta: float = 0.0, v_threshold: float = 0.5, 
                 v_reset: Optional[float] = 0., surrogate_function: Callable = surrogate.Rect()):
        super().__init__(num_node, is_training, T, sigma_init, beta, v_threshold, 
                         v_reset, surrogate_function)

        self.c_decay = c_decay
        self.v_decay = v_decay

    def neuronal_charge(self, x: torch.Tensor):
        self.c = self.c * self.c_decay + x
        self.v = self.v * self.v_decay + self.c

    def init_tensor(self, data: torch.Tensor):
        self.c = torch.full_like(data, fill_value=0.0)
        self.v = torch.full_like(data, fill_value=self.v_reset)


##########################################################################################################
# Inter-Layer Connections (ILC) modules for population-coded spiking actor network
##########################################################################################################

class ILCBaseNode(nn.Module, base.MultiStepModule):
    def __init__(self, act_dim, dec_pop_dim, v_threshold: float = 1.0, v_reset: Optional[float] = 0.,
                 surrogate_function: Callable = surrogate.Rect()):

        assert isinstance(v_reset, float) or v_reset is None
        assert isinstance(v_threshold, float)
        super().__init__()

        self.act_dim = act_dim
        self.out_pop_dim = act_dim * dec_pop_dim
        self.dec_pop_dim = dec_pop_dim

        self.conn = nn.Conv1d(act_dim, self.out_pop_dim, dec_pop_dim, groups=act_dim)

        self.v_threshold = v_threshold
        self.v_reset = v_reset

        self.surrogate_function = surrogate_function

    @abstractmethod
    def neuronal_charge(self, x: torch.Tensor):
        raise NotImplementedError

    def neuronal_fire(self):
        return self.surrogate_function(self.v - self.v_threshold)

    def neuronal_reset(self, spike):
        if self.v_reset is None:
            self.v = self.v - spike * self.v_threshold
        else:
            self.v = (1. - spike) * self.v + spike * self.v_reset

    def init_tensor(self, data: torch.Tensor):
        self.v = torch.full_like(data, fill_value=self.v_reset)

    def forward(self, x_seq: torch.Tensor):
        self.init_tensor(x_seq[0].data)

        T = x_seq.shape[0]
        spike_seq = []

        for t in range(T):
            self.neuronal_charge(x_seq[t])
            spike = self.neuronal_fire()
            self.neuronal_reset(spike)
            spike_seq.append(spike)
            if t < T - 1:
                x_seq[t + 1] = x_seq[t + 1] + self.conn(spike.view(-1, self.act_dim, self.dec_pop_dim)).view(-1, self.out_pop_dim)

        return torch.stack(spike_seq)


class ILCCLIFNode(ILCBaseNode):
    def __init__(self, act_dim, dec_pop_dim, c_decay: float = 0.5, v_decay: float = 0.75,
                 v_threshold: float = 0.5, v_reset: Optional[float] = 0.,
                 surrogate_function: Callable = surrogate.Rect()):

        super().__init__(act_dim, dec_pop_dim, v_threshold, v_reset, surrogate_function)

        self.c_decay = c_decay
        self.v_decay = v_decay

    def neuronal_charge(self, x: torch.Tensor):
        self.c = self.c * self.c_decay + x
        self.v = self.v * self.v_decay + self.c

    def init_tensor(self, data: torch.Tensor):
        self.c = torch.full_like(data, fill_value=0.0)
        self.v = torch.full_like(data, fill_value=self.v_reset)


class ILCLIFNode(ILCBaseNode):
    def __init__(self, act_dim, dec_pop_dim, v_decay: float = 0.75,
                 v_threshold: float = 1.0, v_reset: Optional[float] = 0.,
                 surrogate_function: Callable = surrogate.Rect()):

        super().__init__(act_dim, dec_pop_dim, v_threshold, v_reset, surrogate_function)

        self.v_decay = v_decay

    def neuronal_charge(self, x: torch.Tensor):
        self.v = self.v * self.v_decay + x


class ILCIFNode(ILCBaseNode):
    def __init__(self, act_dim, dec_pop_dim, v_threshold: float = 1.0, v_reset: Optional[float] = 0.,
                 surrogate_function: Callable = surrogate.Rect()):

        super().__init__(act_dim, dec_pop_dim, v_threshold, v_reset, surrogate_function)

    def neuronal_charge(self, x: torch.Tensor):
        self.v = self.v + x


##########################################################################################################
# Noisy modules with Inter-Layer Connections (ILC)
##########################################################################################################

class NoisyILCBaseNode(nn.Module, base.MultiStepModule):
    def __init__(self, act_dim, dec_pop_dim, is_training: bool = True, T: int = 5, 
                 sigma_init: float = 0.5, beta: float = 0.0, v_threshold: float = 1.0, 
                 v_reset: Optional[float] = 0., surrogate_function: Callable = surrogate.Rect()):

        assert isinstance(v_reset, float) or v_reset is None
        assert isinstance(v_threshold, float)
        super().__init__()

        self.act_dim = act_dim
        self.num_node = act_dim * dec_pop_dim
        self.dec_pop_dim = dec_pop_dim

        self.conn = nn.Conv1d(act_dim, self.num_node, dec_pop_dim, groups=act_dim)

        self.is_training = is_training
        self.T = T
        self.beta = beta

        self.sigma_v = sigma_init / math.sqrt(self.num_node)
        self.cn_v = None

        self.sigma_s = sigma_init / math.sqrt(self.num_node)
        self.cn_s = None

        self.v_threshold = v_threshold
        self.v_reset = v_reset

        self.surrogate_function = surrogate_function

    @abstractmethod
    def neuronal_charge(self, x: torch.Tensor):
        raise NotImplementedError

    def neuronal_fire(self):
        return self.surrogate_function(self.v - self.v_threshold)

    def neuronal_reset(self, spike):
        if self.v_reset is None:
            self.v = self.v - spike * self.v_threshold
        else:
            self.v = (1. - spike) * self.v + spike * self.v_reset

    def init_tensor(self, data: torch.Tensor):
        self.v = torch.full_like(data, fill_value=self.v_reset)

    def forward(self, x_seq: torch.Tensor):
        self.init_tensor(x_seq[0].data)

        y = []

        if self.is_training:
            if self.cn_v is None or self.cn_s is None:
                self.noise_step += 1

            for t in range(self.T):      
                if self.cn_v is None:
                    self.neuronal_charge(x_seq[t] + self.sigma_v * self.eps_v_seq[self.noise_step][t].to(x_seq.device))
                else:
                    self.neuronal_charge(x_seq[t] + self.sigma_v * self.cn_v[:, t])
                spike = self.neuronal_fire()
                self.neuronal_reset(spike)
                if self.cn_s is None:
                    spike = spike + self.sigma_s * self.eps_s_seq[self.noise_step][t].to(x_seq.device)
                else:
                    spike = spike + self.sigma_s * self.cn_s[:, t]
                y.append(spike)

                if t < self.T - 1:
                    x_seq[t + 1] = x_seq[t + 1] + self.conn(spike.view(-1, self.act_dim, self.dec_pop_dim)).view(-1, self.num_node)
            
        else:
            for t in range(self.T):
                self.neuronal_charge(x_seq[t])
                spike = self.neuronal_fire()
                self.neuronal_reset(spike)
                y.append(spike)

                if t < self.T - 1:
                    x_seq[t + 1] = x_seq[t + 1] + self.conn(spike.view(-1, self.act_dim, self.dec_pop_dim)).view(-1, self.num_node)

        return torch.stack(y)
        
    def reset_noise(self, num_rl_step):
        eps_shape = [self.num_node, num_rl_step * self.T]
        per_order = [1, 2, 0]
        # (nodes, steps * T) -> (nodes, steps, T) -> (steps, T, nodes)
        self.eps_v_seq = torch.FloatTensor(powerlaw_psd_gaussian(self.beta, eps_shape).reshape(self.num_node, num_rl_step, self.T)).permute(per_order)
        self.eps_s_seq = torch.FloatTensor(powerlaw_psd_gaussian(self.beta, eps_shape).reshape(self.num_node, num_rl_step, self.T)).permute(per_order)
        self.noise_step = -1

    def get_colored_noise(self):
        cn = [self.eps_v_seq[self.noise_step], self.eps_s_seq[self.noise_step]]
        return torch.cat(cn, dim=1)

    def load_colored_noise(self, cn):
        self.cn_v = cn[:, :, :self.num_node]
        self.cn_s = cn[:, :, self.num_node:]

    def cancel_load(self):
        self.cn_v = None
        self.cn_s = None


class NoisyILCCLIFNode(NoisyILCBaseNode):
    def __init__(self, act_dim, dec_pop_dim, c_decay: float = 0.5, v_decay: float = 0.75,
                 is_training: bool = True, T: int = 5, sigma_init: float = 0.5, 
                 beta: float = 0.0, v_threshold: float = 1.0, v_reset: Optional[float] = 0.,
                 surrogate_function: Callable = surrogate.Rect()):
        super().__init__(act_dim, dec_pop_dim, is_training, T, sigma_init, beta, v_threshold, 
                         v_reset, surrogate_function)

        self.c_decay = c_decay
        self.v_decay = v_decay

    def neuronal_charge(self, x: torch.Tensor):
        self.c = self.c * self.c_decay + x
        self.v = self.v * self.v_decay + self.c

    def init_tensor(self, data: torch.Tensor):
        self.c = torch.full_like(data, fill_value=0.0)
        self.v = torch.full_like(data, fill_value=self.v_reset)


##########################################################################################################
# Non-spiking modules for floating-point output
##########################################################################################################

class NonSpikingBaseNode(nn.Module, base.MultiStepModule):
    def __init__(self, decode='last-mem'):
        super().__init__()

        self.decode = decode

    @abstractmethod
    def neuronal_charge(self, x: torch.Tensor):
        raise NotImplementedError

    def forward(self, x_seq: torch.Tensor):
        self.v = torch.full_like(x_seq[0].data, fill_value=0.0)

        T = x_seq.shape[0]
        v_seq = []

        for t in range(T):
            self.neuronal_charge(x_seq[t])
            v_seq.append(self.v)

        if self.decode == 'max-mem':
            mem = torch.max(torch.stack(v_seq, 0), 0).values

        elif self.decode == 'max-abs-mem':
            v_stack = torch.stack(v_seq, 0)
            max_mem = torch.max(v_stack, 0).values
            min_mem = torch.min(v_stack, 0).values
            mem = max_mem * (max_mem.abs() > min_mem.abs()) + min_mem * (max_mem.abs() <= min_mem.abs())

        elif self.decode == 'mean-mem':
            mem = torch.mean(torch.stack(v_seq, 0), 0)

        else:  # 'last-mem'
            mem = v_seq[-1]

        return mem


class NonSpikingIFNode(NonSpikingBaseNode):
    def __init__(self, decode='last-mem'):
        super().__init__(decode)

    def neuronal_charge(self, x: torch.Tensor):
        self.v = self.v + x


class NonSpikingLIFNode(NonSpikingBaseNode):
    def __init__(self, tau: float = 2., decode='last-mem'):
        super().__init__(decode)

        self.tau = tau

    def neuronal_charge(self, x: torch.Tensor):
        self.v = self.v + (x - self.v) / self.tau


##########################################################################################################
# Noisy Non-spiking modules
##########################################################################################################

class NoisyNonSpikingBaseNode(nn.Module, base.MultiStepModule):
    def __init__(self, num_node, is_training: bool = True, T: int = 5, 
                 sigma_init: float = 0.5, beta: float = 0.0, decode: str = 'last-mem'):
        super().__init__()

        self.num_node = num_node
        self.is_training = is_training
        self.T = T
        self.beta = beta
        self.decode = decode

        self.sigma = nn.Parameter(torch.FloatTensor(num_node))
        self.sigma.data.fill_(sigma_init / math.sqrt(num_node))
        self.cn = None

    @abstractmethod
    def neuronal_charge(self, x: torch.Tensor):
        raise NotImplementedError

    def init_tensor(self, data: torch.Tensor):
        self.v = torch.full_like(data, fill_value=0.0)

    def forward(self, x_seq: torch.Tensor):
        self.init_tensor(x_seq[0].data)

        v_seq = []

        if self.is_training:
            if self.cn is None:
                self.noise_step += 1

            for t in range(self.T):
                if self.cn is None:
                    self.neuronal_charge(x_seq[t] + self.sigma.mul(self.eps_seq[self.noise_step][t].to(x_seq.device)))
                else:
                    self.neuronal_charge(x_seq[t] + self.sigma.mul(self.cn[:, t].to(x_seq.device)))
                v_seq.append(self.v)
                
        else:
            for t in range(self.T):
                self.neuronal_charge(x_seq[t])
                v_seq.append(self.v)

        if self.decode == 'max-mem':
            mem = torch.max(torch.stack(v_seq, 0), 0).values

        elif self.decode == 'max-abs-mem':
            v_stack = torch.stack(v_seq, 0)
            max_mem = torch.max(v_stack, 0).values
            min_mem = torch.min(v_stack, 0).values
            mem = max_mem * (max_mem.abs() > min_mem.abs()) + min_mem * (max_mem.abs() <= min_mem.abs())

        elif self.decode == 'mean-mem':
            mem = torch.mean(torch.stack(v_seq, 0), 0)

        else:  # 'last-mem'
            mem = v_seq[-1]

        return mem

    def reset_noise(self, num_rl_step):
        eps_shape = [self.num_node, num_rl_step * self.T]
        per_order = [1, 2, 0]
        self.eps_seq = torch.FloatTensor(powerlaw_psd_gaussian(self.beta, eps_shape).reshape(self.num_node, num_rl_step, self.T)).permute(per_order)
        self.noise_step = -1

    def get_colored_noise(self):
        return self.eps_seq[self.noise_step]

    def load_colored_noise(self, cn):
        self.cn = cn

    def cancel_load(self):
        self.cn = None


class NoisyNonSpikingIFNode(NoisyNonSpikingBaseNode):
    def neuronal_charge(self, x: torch.Tensor):
        self.v = self.v + x


##########################################################################################################
# Membrane Potential Batch Normalization and Threshold Modulation modules
##########################################################################################################

class MPBNBaseNode(BaseNode):
    def __init__(self, v_threshold: float = 1., v_reset: Optional[float] = 0.,
                 surrogate_function: Callable = surrogate.Sigmoid(), detach_reset: bool = False,
                 step_mode = 's', backend = 'torch', store_v_seq: bool = False, mpbn: bool = True, 
                 out_features = None, out_channels = None, learnable_vth: bool = False,
                 bn_momentum: float = 0.1, bn_decay_momentum: float = 0.94, bn_min_momentum: float = 0.005):
        r"""
        * :ref:`API in English <MPBNBaseNode.__init__-en>`

        .. _MPBNBaseNode.__init__-cn:

        :param mpbn: 是否启用MPBN
        :type mpbn: bool

        :param out_features: 特征维度，用于线性层后
        :type out_features: int

        :param out_channels: 特征通道数，用于2D卷积层后
        :type out_channels: int

        :param learnable_vth: 阈值是否可训练
        :type learnable_vth: bool

        :param bn_momentum: 阈值重参数化后，更新统计量时使用的动量
        :type bn_momentum: float

        :param bn_decay_momentum: 阈值重参数化后，更新统计量时使用的动量衰减
        :type bn_decay_momentum: float

        :param bn_min_momentum: 阈值重参数化后，更新统计量时使用的最小动量
        :type bn_min_momentum: float
        其余参数与 :class:`BaseNode` 相同。

        `Membrane Potential Batch Normalization for Spiking Neural Networks <https://arxiv.org/abs/2308.08359>` 提出的对膜电压进行批量归一化的神经元模型基类。
        `Threshold Modulation for Online Test-Time Adaptation of Spiking Neural Networks <https://arxiv.org/abs/2505.05375>` 在此基础上引入阈值调制模块来进行测试时适应任务并降低能耗。

        .. math::
            :nowrap:
            \begin{aligned}
                H'[t] &= \mathbf{BN}(H[t]) && \text{（训练时）} \\
                (\tilde{V}_{th})_{i} &= \frac{(V_{th}-\beta_{i})\sqrt{\sigma_{i}^{2}}}{\gamma_{i}}+\mu_{i} && \text{（测试时适应）}
            \end{aligned}
        
        * :ref:`中文API <MPBNBaseNode.__init__-cn>`

        .. _MPBNBaseNode.__init__-en:

        :param mpbn: whether to enable MPBN
        :type mpbn: bool

        :param out_features: feature dimension, when used after `Linear`
        :type out_features: int

        :param out_channels: number of channels, when used after `Conv2d` 
        :type out_channels: int

        :param learnable_vth: whether to train a (positive) threshold
        :type learnable_vth: bool

        :param bn_momentum: the momentum used in statistics update after threshold re-parameterization
        :type bn_momentum: float

        :param bn_decay_momentum: the momentum decay used in statistics update after threshold re-parameterization
        :type bn_decay_momentum: float

        :param bn_min_momentum: the minimum momentum used in statistics update after threshold re-parameterization
        :type bn_min_momentum: float
        Other parameters are the same as :class:`BaseNode`.

        Base class of neuron with membrane potential batch normalization proposed in `Membrane Potential Batch Normalization for Spiking Neural Networks <https://arxiv.org/abs/2308.08359>`.
        `Threshold Modulation for Online Test-Time Adaptation of Spiking Neural Networks <https://arxiv.org/abs/2505.05375>` further introduces a Threshold Modulation module after threshold re-parameterization 
        to enable test-time adaptation and reduce energy consumption.

        .. math::
            :nowrap:
            \begin{aligned}
                H'[t] &= \mathbf{BN}(H[t]) && \text{(training)} \\
                (\tilde{V}_{th})_{i} &= \frac{(V_{th}-\beta_{i})\sqrt{\sigma_{i}^{2}}}{\gamma_{i}}+\mu_{i} && \text{(test-time adaptation)}
            \end{aligned}

        """
        super().__init__(v_threshold, v_reset, surrogate_function, detach_reset, step_mode, backend, store_v_seq)
        assert out_features is None and out_channels is not None or out_features is not None and out_channels is None, \
            "One of out_features or out_channels should be specified."
        self.out_features = out_features
        self.out_channels = out_channels
        self.mpbn = mpbn
        if mpbn:
            if out_features is None and out_channels is not None:
                self.vbn = nn.LazyBatchNorm2d()
            else:
                self.vbn = nn.LazyBatchNorm1d()
        else:
            self.vbn = nn.Identity()

        self.register_buffer('mu', None)
        self.register_buffer('sigma2', None)
        self.gamma = None
        self.beta = None
        self.eps = None

        self.fold_bn = False
        self.normalize_residual = False
        self.running_stats = False

        self.bn_momentum = bn_momentum
        self.bn_decay_momentum = bn_decay_momentum
        self.bn_min_momentum = bn_min_momentum

        self.learnable_vth = learnable_vth
        if learnable_vth:  # force the threshold to be positive
            self.a = nn.Parameter(torch.full((out_channels or out_features,), 0.))
        
        self.register_memory('vth', v_threshold)

    def init_vth(self, x: torch.Tensor):
        if isinstance(self.vth, float):
            if isinstance(self.v_threshold, float):
                self.vth = torch.full((x.shape[1],), self.v_threshold, device=x.device, dtype=x.dtype)
            else:
                self.vth = self.v_threshold
            self.vth_ = self.vth
    
    def compute_running_stats(self, v: torch.Tensor):  # you can disable this completely by overiding it in subclasses
        if v.ndim == 2:
            if v.shape[0] == 1:
                return
            mu = torch.mean(v, dim=0).detach()
            sigma2 = torch.var(v, dim=0, unbiased=True).detach()
            if self.running_stats:
                if self.mu is None or self.sigma2 is None:
                    self.mu = mu
                    self.sigma2 = sigma2
                else:
                    self.mu = self.mu.detach() * (1 - self.bn_momentum) + mu * self.bn_momentum
                    self.sigma2 = self.sigma2.detach() * (1 - self.bn_momentum) + sigma2 * self.bn_momentum
                    self.bn_momentum = max(self.bn_momentum * self.bn_decay_momentum, self.bn_min_momentum)
            else:
                self.mu = mu
                self.sigma2 = sigma2
        elif v.ndim == 4:
            mu = torch.mean(v, dim=(0, 2, 3)).detach()
            sigma2 = torch.var(v, dim=(0, 2, 3), unbiased=True).detach()
            if self.running_stats:
                if self.mu is None or self.sigma2 is None:
                    self.mu = mu
                    self.sigma2 = sigma2
                else:
                    self.mu = self.mu.detach() * (1 - self.bn_momentum) + mu * self.bn_momentum
                    self.sigma2 = self.sigma2.detach() * (1 - self.bn_momentum) + sigma2 * self.bn_momentum
                    self.bn_momentum = max(self.bn_momentum * self.bn_decay_momentum, self.bn_min_momentum)
            else:
                self.mu = mu
                self.sigma2 = sigma2
        else:
            raise NotImplementedError(f"Only 2D and 4D tensor are supported, but got {v.ndim}D tensor.")
    
    def pre_charge(self, x: torch.Tensor):
        raise NotImplementedError("This method should be implemented in subclasses, e.g. the charging function of LIF neuron.")

    def neuronal_charge(self, x: torch.Tensor):
        self.pre_charge(x)
        self.v = self.vbn(self.v)
        if self.fold_bn and not self.learnable_vth and self.training:
            self.compute_running_stats(self.v)

    def neuronal_fire(self):
        if self.v.ndim == 2:
            if self.fold_bn and not self.learnable_vth:
                self.vth = (self.vth_ - self.beta) * torch.sqrt(self.sigma2 + self.eps) / self.gamma + self.mu
            if self.learnable_vth:
                self.vth = torch.exp(self.a)
            diff = self.v - self.vth.view(1, self.vth.shape[0])
            spike = self.surrogate_function(diff)
            if self.normalize_residual:
                mask = diff <= 0
                gamma = self.gamma.unsqueeze(0).expand_as(mask)
                mu = self.mu.unsqueeze(0).expand_as(mask)
                beta = self.beta.unsqueeze(0).expand_as(mask)
                sigma = torch.sqrt(self.sigma2 + self.eps).unsqueeze(0).expand_as(mask)
                normalized_residual = (self.v[mask] - mu[mask]) / sigma[mask] * gamma[mask] + beta[mask]
                self.v.masked_scatter_(mask, normalized_residual)
        elif self.v.ndim == 4:
            if self.fold_bn and not self.learnable_vth:
                self.vth = (self.vth_ - self.beta) * torch.sqrt(self.sigma2 + self.eps) / self.gamma + self.mu
            if self.learnable_vth:
                self.vth = torch.exp(self.a)
            diff = self.v - self.vth.view(1, self.vth.shape[0], 1, 1)
            spike = self.surrogate_function(diff)
            if self.normalize_residual:
                mask = diff <= 0
                gamma = self.gamma.view(1, -1, 1, 1).expand_as(mask)
                mu = self.mu.view(1, -1, 1, 1).expand_as(mask)
                beta = self.beta.view(1, -1, 1, 1).expand_as(mask)
                sigma = torch.sqrt(self.sigma2 + self.eps).view(1, -1, 1, 1).expand_as(mask)
                normalized_residual = (self.v[mask] - mu[mask]) / sigma[mask] * gamma[mask] + beta[mask]
                self.v.masked_scatter_(mask, normalized_residual)
        else:
            raise NotImplementedError(f"Only 2D and 4D tensors are supported, but got {self.v.ndim}D tensors.")
        return spike

    def single_step_forward(self, x: torch.Tensor):
        self.init_vth(x)
        self.v_float_to_tensor(x)
        self.neuronal_charge(x)
        spike = self.neuronal_fire()
        self.neuronal_reset(spike)
        return spike

    def re_parameterize_v_threshold(self, normalize_residual: bool = False, running_stats: bool = False):
        # "re-parameterize" threshold to enable TTA capability
        if isinstance(self.vbn, nn.Identity):
            if self.fold_bn == True:
                print(f"Re-parameterization has already been done in this neuron, skipping...")
            else:
                print(f"MPBN is not enabled in this neuron, skipping...")
            return
        self.fold_bn = True
        if self.learnable_vth:  # if self.a is learned during training:
            with torch.no_grad():
                self.v_threshold = torch.exp(self.a)
            self.learnable_vth = False
        self.normalize_residual = normalize_residual
        self.running_stats = running_stats
        self.mu = self.vbn.running_mean
        self.sigma2 = self.vbn.running_var
        self.gamma = nn.Parameter(self.vbn.weight)
        self.beta = nn.Parameter(self.vbn.bias)
        self.eps = self.vbn.eps
        self.vbn = nn.Identity()


class MPBNLIFNode(MPBNBaseNode):
    def __init__(self, tau: float = 2., decay_input: bool = False, v_threshold: float = 1.,
                 v_reset: Optional[float] = 0., surrogate_function: Callable = surrogate.Sigmoid(),
                 detach_reset: bool = False, step_mode = 's', backend = 'torch', store_v_seq: bool = False,
                 mpbn: bool = True, out_features = None, out_channels = None, learnable_vth: bool = False,
                 bn_momentum: float = 0.1, bn_decay_momentum: float = 0.94, bn_min_momentum: float = 0.005):
        r"""
        * :ref:`API in English <MPBNLIFNode.__init__-en>`

        .. _MPBNLIFNode.__init__-cn:

        :param tau: LIF中的时间常数
        :type tau: float

        :param decay_input: 输入是否参与衰减
        :type decay_input: bool
        其余参数与 :class:`MPBNBaseNode` 相同。

        `Membrane Potential Batch Normalization for Spiking Neural Networks <https://arxiv.org/abs/2308.08359>` 中使用的对膜电压进行批量归一化的LIF神经元模型。
        `Threshold Modulation for Online Test-Time Adaptation of Spiking Neural Networks <https://arxiv.org/abs/2505.05375>` 在此基础上引入阈值调制模块来进行测试时适应任务并降低能耗。

        .. math::
            :nowrap:
            \begin{aligned}
                H'[t] &= \mathbf{BN}(H[t]) && \text{（训练时）} \\
                (\tilde{V}_{th})_{i} &= \frac{(V_{th}-\beta_{i})\sqrt{\sigma_{i}^{2}}}{\gamma_{i}}+\mu_{i} && \text{（测试时适应）}
            \end{aligned}
        
        * :ref:`中文API <MPBNLIFNode.__init__-cn>`

        .. _MPBNLIFNode.__init__-en:

        :param tau: time constant in LIF
        :type tau: float

        :param decay_input: whether the input current is decayed
        :type decay_input: bool
        Other parameters are the same as :class:`MPBNBaseNode`.

        LIF neuron with membrane potential batch normalization used in `Membrane Potential Batch Normalization for Spiking Neural Networks <https://arxiv.org/abs/2308.08359>`.
        `Threshold Modulation for Online Test-Time Adaptation of Spiking Neural Networks <https://arxiv.org/abs/2505.05375>` further introduces a Threshold Modulation module after threshold re-parameterization.

        .. math::
            :nowrap:
            \begin{aligned}
                H'[t] &= \mathbf{BN}(H[t]) && \text{(training)} \\
                (\tilde{V}_{th})_{i} &= \frac{(V_{th}-\beta_{i})\sqrt{\sigma_{i}^{2}}}{\gamma_{i}}+\mu_{i} && \text{(test-time adaptation)}
            \end{aligned}

        """
        assert isinstance(tau, float) and tau > 1.
        super().__init__(v_threshold, v_reset, surrogate_function, detach_reset, step_mode, backend, store_v_seq,
                         mpbn, out_features, out_channels, learnable_vth, bn_momentum, bn_decay_momentum, bn_min_momentum)

        self.tau = tau
        self.decay_input = decay_input
    
    @property
    def supported_backends(self):
        return ('torch',)

    def pre_charge(self, x: torch.Tensor):
        if self.decay_input:
            if self.v_reset is None or self.v_reset == 0.:
                self.v = self.neuronal_charge_decay_input_reset0(x, self.v, self.tau)
            else:
                self.v = self.neuronal_charge_decay_input(x, self.v, self.v_reset, self.tau)
        else:
            if self.v_reset is None or self.v_reset == 0.:
                self.v = self.neuronal_charge_no_decay_input_reset0(x, self.v, self.tau)
            else:
                self.v = self.neuronal_charge_no_decay_input(x, self.v, self.v_reset, self.tau)

    @staticmethod
    @torch.jit.script
    def neuronal_charge_decay_input_reset0(x: torch.Tensor, v: torch.Tensor, tau: float):
        v = v + (x - v) / tau
        return v

    @staticmethod
    @torch.jit.script
    def neuronal_charge_decay_input(x: torch.Tensor, v: torch.Tensor, v_reset: float, tau: float):
        v = v + (x - (v - v_reset)) / tau
        return v

    @staticmethod
    @torch.jit.script
    def neuronal_charge_no_decay_input_reset0(x: torch.Tensor, v: torch.Tensor, tau: float):
        v = v * (1. - 1. / tau) + x
        return v

    @staticmethod
    @torch.jit.script
    def neuronal_charge_no_decay_input(x: torch.Tensor, v: torch.Tensor, v_reset: float, tau: float):
        v = v - (v - v_reset) / tau + x
        return v
