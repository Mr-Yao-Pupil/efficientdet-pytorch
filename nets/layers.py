import re
import math
import collections
from functools import partial
import torch
from torch import nn
from torch.nn import functional as F
from torch.utils import model_zoo

# --------------------------------------------------------------#
#   模型构建的辅助函数
# --------------------------------------------------------------#

# 对于collections.namedtuple的使用见
GlobalParams = collections.namedtuple('GlobalParams', [
    'batch_norm_momentum', 'batch_norm_epsilon', 'dropout_rate',
    'num_classes', 'width_coefficient', 'depth_coefficient',
    'depth_divisor', 'min_depth', 'drop_connect_rate', 'image_size'])

BlockArgs = collections.namedtuple('BlockArgs', [
    'kernel_size', 'num_repeat', 'input_filters', 'output_filters',
    'expand_ratio', 'id_skip', 'stride', 'se_ratio'])

GlobalParams.__new__.__defaults__ = (None,) * len(GlobalParams._fields)
BlockArgs.__new__.__defaults__ = (None,) * len(BlockArgs._fields)


def round_filters(filters, global_params):
    """ Calculate and round number of filters based on depth multiplier. """
    multiplier = global_params.width_coefficient
    if not multiplier:
        return filters
    divisor = global_params.depth_divisor
    min_depth = global_params.min_depth
    filters *= multiplier
    min_depth = min_depth or divisor
    new_filters = max(min_depth, int(filters + divisor / 2) // divisor * divisor)
    if new_filters < 0.9 * filters:  # prevent rounding by more than 10%
        new_filters += divisor
    return int(new_filters)


def round_repeats(repeats, global_params):
    """ Round number of filters based on depth multiplier. """
    multiplier = global_params.depth_coefficient
    if not multiplier:
        return repeats
    return int(math.ceil(multiplier * repeats))


def drop_connect(inputs, p, training):
    """ Drop connect. """
    if not training: return inputs
    batch_size = inputs.shape[0]
    keep_prob = 1 - p
    random_tensor = keep_prob
    random_tensor += torch.rand([batch_size, 1, 1, 1], dtype=inputs.dtype, device=inputs.device)
    binary_tensor = torch.floor(random_tensor)
    output = inputs / keep_prob * binary_tensor
    return output


def get_same_padding_conv2d(image_size=None):
    """ Chooses static padding if you have specified an image size, and dynamic padding otherwise.
        Static padding is necessary for ONNX exporting of models. """
    if image_size is None:
        return Conv2dDynamicSamePadding
    else:
        return partial(Conv2dStaticSamePadding, image_size=image_size)


class Conv2dDynamicSamePadding(nn.Conv2d):
    """定义一个类似于TensorFlow2中Conv2d的卷积类"""

    def __init__(self, in_channels, out_channels, kernel_size, stride=1, dilation=1, groups=1, bias=True):
        super().__init__(in_channels, out_channels, kernel_size, stride, 0, dilation, groups, bias)
        self.stride = self.stride if len(self.stride) == 2 else [self.stride[0]] * 2

    def forward(self, x):
        ih, iw = x.size()[-2:]
        kh, kw = self.weight.size()[-2:]
        sh, sw = self.stride
        oh, ow = math.ceil(ih / sh), math.ceil(iw / sw)
        pad_h = max((oh - 1) * self.stride[0] + (kh - 1) * self.dilation[0] + 1 - ih, 0)
        pad_w = max((ow - 1) * self.stride[1] + (kw - 1) * self.dilation[1] + 1 - iw, 0)
        if pad_h > 0 or pad_w > 0:
            x = F.pad(x, [pad_w // 2, pad_w - pad_w // 2, pad_h // 2, pad_h - pad_h // 2])
        return F.conv2d(x, self.weight, self.bias, self.stride, self.padding, self.dilation, self.groups)


class Identity(nn.Module):
    def __init__(self, ):
        super(Identity, self).__init__()

    def forward(self, input):
        return input


# --------------------------------------------------------------#
#   加载模型参数的辅助函数
# --------------------------------------------------------------#
def efficientnet_params(model_name):
    """
    获取网络的形状变化信息

    :param model_name: 网络型号选择，从efficientnet-b{0~7}
    :return: 长度为4的网络参数信息，从左至右为网络的宽度变化比例，网络深度变化比例，输入图片的长宽信息，Dropout的参数失活比例
    """
    params_dict = {
        # Coefficients:   width,depth,res,dropout
        'efficientnet-b0': (1.0, 1.0, 224, 0.2),
        'efficientnet-b1': (1.0, 1.1, 240, 0.2),
        'efficientnet-b2': (1.1, 1.2, 260, 0.3),
        'efficientnet-b3': (1.2, 1.4, 300, 0.3),
        'efficientnet-b4': (1.4, 1.8, 380, 0.4),
        'efficientnet-b5': (1.6, 2.2, 456, 0.4),
        'efficientnet-b6': (1.8, 2.6, 528, 0.5),
        'efficientnet-b7': (2.0, 3.1, 600, 0.5),
        'efficientnet-b8': (2.2, 3.6, 672, 0.5),
        'efficientnet-l2': (4.3, 5.3, 800, 0.5),
    }
    return params_dict[model_name]


class BlockDecoder(object):
    """ Block Decoder for readability, straight from the official TensorFlow repository """

    @staticmethod
    def _decode_block_string(block_string):
        """
        对传入的字符串进行正则化解码，对一个字符串返回一个 BlockArgs
        :param block_string: 包含模型内模块信息的字符串，编码方式说明如下
        :return: 解码字符串后的一个模块参数信息，其结构如下
                BlockArgs(kernel_size=int(options['k']),
                          num_repeat=int(options['r']),
                          input_filters=int(options['i']),
                          output_filters=int(options['o']),
                          expand_ratio=int(options['e']),
                          id_skip=('noskip' not in block_string),
                          se_ratio=float(options['se']) if 'se' in options else None,
                          stride=[int(options['s'][0])])

            例如输入字符串为:'r1_k3_s11_e1_i32_o16_se0.25'
            返回结果为:BlockArgs(kernel_size=3,
                               num_repeat=1,
                               input_filters=32,
                               output_filters=16,
                               expand_ratio=1,
                               id_skip=True,
                               se_ratio=0.25,
                               stride=1)
        """
        assert isinstance(block_string, str)

        ops = block_string.split('_')
        options = {}
        for op in ops:
            splits = re.split(r'(\d.*)', op)
            if len(splits) >= 2:
                key, value = splits[:2]
                options[key] = value

        # Check stride
        assert (('s' in options and len(options['s']) == 1) or
                (len(options['s']) == 2 and options['s'][0] == options['s'][1]))

        return BlockArgs(
            kernel_size=int(options['k']),
            num_repeat=int(options['r']),
            input_filters=int(options['i']),
            output_filters=int(options['o']),
            expand_ratio=int(options['e']),
            id_skip=('noskip' not in block_string),
            se_ratio=float(options['se']) if 'se' in options else None,
            stride=[int(options['s'][0])])

    @staticmethod
    def _encode_block_string(block):
        """Encodes a block to a string."""
        args = [
            'r%d' % block.num_repeat,
            'k%d' % block.kernel_size,
            's%d%d' % (block.strides[0], block.strides[1]),
            'e%s' % block.expand_ratio,
            'i%d' % block.input_filters,
            'o%d' % block.output_filters
        ]
        if 0 < block.se_ratio <= 1:
            args.append('se%s' % block.se_ratio)
        if block.id_skip is False:
            args.append('noskip')
        return '_'.join(args)

    @staticmethod
    def decode(string_list):
        """
        解码一个包含多个字符串的列表，返回一个包含每个字符串解码后的collections.namedtuple.

        :param string_list: 一个包含多个字符串的列表，每个字符串包含未解码的模块生成信息
        :return: 包含efficiennet的多个BlockArgs的列表
        """
        assert isinstance(string_list, list)
        blocks_args = []
        for block_string in string_list:
            blocks_args.append(BlockDecoder._decode_block_string(block_string))
        return blocks_args

    @staticmethod
    def encode(blocks_args):
        """
        Encodes a list of BlockArgs to a list of strings.

        :param blocks_args: a list of BlockArgs namedtuples of block args
        :return: a list of strings, each string is a notation of block
        """
        block_strings = []
        for block in blocks_args:
            block_strings.append(BlockDecoder._encode_block_string(block))
        return block_strings


def efficientnet(width_coefficient=None, depth_coefficient=None, dropout_rate=0.2,
                 drop_connect_rate=0.2, image_size=None, num_classes=1000):
    """
    返回一个efficiennet模型的参数，和模型整体细节

    :param width_coefficient: 在efficiennet基础模型上的宽度变化
    :param depth_coefficient: 在efficiennet基础模型上的深度变化
    :param dropout_rate: 训练模型时nn.dorpout的比例参数
    :param drop_connect_rate:
    :param image_size: 输入efficiennet模型的图片大小
    :param num_classes: efficiennet模型最后输出的分类数目
    :return:
    """

    blocks_args = [
        'r1_k3_s11_e1_i32_o16_se0.25', 'r2_k3_s22_e6_i16_o24_se0.25',
        'r2_k5_s22_e6_i24_o40_se0.25', 'r3_k3_s22_e6_i40_o80_se0.25',
        'r3_k5_s11_e6_i80_o112_se0.25', 'r4_k5_s22_e6_i112_o192_se0.25',
        'r1_k3_s11_e6_i192_o320_se0.25',
    ]
    # 将blocks_args正则化解码,通过BlockDecoder获取一个类似于字典的collections.namedtuple
    blocks_args = BlockDecoder.decode(blocks_args)

    global_params = GlobalParams(
        batch_norm_momentum=0.99,
        batch_norm_epsilon=1e-3,
        dropout_rate=dropout_rate,
        drop_connect_rate=drop_connect_rate,
        # data_format='channels_last',  # removed, this is always true in PyTorch
        num_classes=num_classes,
        width_coefficient=width_coefficient,
        depth_coefficient=depth_coefficient,
        depth_divisor=8,
        min_depth=None,
        image_size=image_size,
    )

    return blocks_args, global_params


def get_model_params(model_name, override_params):
    """
    获取网络的模块信息和全局参数

    :param model_name:  网络型号选择，从efficientnet-b{0~7}
    :param override_params: 网络的其他参数，如分类层分类数目
    :return: 网络的内部子模块信息参数和网络全局参数，数据类型为collections.namedtuple
    """
    if model_name.startswith('efficientnet'):
        w, d, s, p = efficientnet_params(model_name)
        # note: all models have drop connect rate = 0.2
        blocks_args, global_params = efficientnet(
            width_coefficient=w, depth_coefficient=d, dropout_rate=p, image_size=s)
    else:
        raise NotImplementedError('model name is not pre-defined: %s' % model_name)
    if override_params:
        # ValueError will be raised here if override_params has fields not included in global_params.
        global_params = global_params._replace(**override_params)
    return blocks_args, global_params


url_map = {
    'efficientnet-b0': 'https://publicmodels.blob.core.windows.net/container/aa/efficientnet-b0-355c32eb.pth',
    'efficientnet-b1': 'https://publicmodels.blob.core.windows.net/container/aa/efficientnet-b1-f1951068.pth',
    'efficientnet-b2': 'https://publicmodels.blob.core.windows.net/container/aa/efficientnet-b2-8bb594d6.pth',
    'efficientnet-b3': 'https://publicmodels.blob.core.windows.net/container/aa/efficientnet-b3-5fb5a3c3.pth',
    'efficientnet-b4': 'https://publicmodels.blob.core.windows.net/container/aa/efficientnet-b4-6ed6700e.pth',
    'efficientnet-b5': 'https://publicmodels.blob.core.windows.net/container/aa/efficientnet-b5-b6417697.pth',
    'efficientnet-b6': 'https://publicmodels.blob.core.windows.net/container/aa/efficientnet-b6-c76e70fd.pth',
    'efficientnet-b7': 'https://publicmodels.blob.core.windows.net/container/aa/efficientnet-b7-dcc49843.pth',
}

url_map_advprop = {
    'efficientnet-b0': 'https://publicmodels.blob.core.windows.net/container/advprop/efficientnet-b0-b64d5a18.pth',
    'efficientnet-b1': 'https://publicmodels.blob.core.windows.net/container/advprop/efficientnet-b1-0f3ce85a.pth',
    'efficientnet-b2': 'https://publicmodels.blob.core.windows.net/container/advprop/efficientnet-b2-6e9d97e5.pth',
    'efficientnet-b3': 'https://publicmodels.blob.core.windows.net/container/advprop/efficientnet-b3-cdd7c0f4.pth',
    'efficientnet-b4': 'https://publicmodels.blob.core.windows.net/container/advprop/efficientnet-b4-44fb3a87.pth',
    'efficientnet-b5': 'https://publicmodels.blob.core.windows.net/container/advprop/efficientnet-b5-86493f6b.pth',
    'efficientnet-b6': 'https://publicmodels.blob.core.windows.net/container/advprop/efficientnet-b6-ac80338e.pth',
    'efficientnet-b7': 'https://publicmodels.blob.core.windows.net/container/advprop/efficientnet-b7-4652b6dd.pth',
    'efficientnet-b8': 'https://publicmodels.blob.core.windows.net/container/advprop/efficientnet-b8-22a8fe65.pth',
}


def load_pretrained_weights(model, model_name, load_fc=True, advprop=False):
    """
    用于加载与训练权重，如果是第一次加载与训练权重会下载

    :param model: 实例化后的网络
    :param model_name: 实例化是网络的型号，从 efficientnet-b{0~7}
    :param load_fc: 是否加载分类曾权重，默认加载
    :param advprop: 预训练权重下载地址选择，默认为False，此时选择url_map_advprop，为True时选择url_map
    """
    # AutoAugment or Advprop (different preprocessing)
    # 预备训练权重的下载地址选择
    url_map_ = url_map_advprop if advprop else url_map
    state_dict = model_zoo.load_url(url_map_[model_name], map_location=torch.device('cpu'))
    # state_dict = torch.load('../../weights/backbone_efficientnetb0.pth')
    if load_fc:
        ret = model.load_state_dict(state_dict, strict=False)
        print(ret)
    else:
        # 不加载全连接层则将权重弹出后加载主网络权重，若pop失败则出发断言机制
        state_dict.pop('_fc.weight')
        state_dict.pop('_fc.bias')
        res = model.load_state_dict(state_dict, strict=False)
        assert set(res.missing_keys) == set(['_fc.weight', '_fc.bias']), 'issue loading pretrained weights'
    print('{}预训练权重加载完成'.format(model_name))


class SwishImplementation(torch.autograd.Function):
    @staticmethod
    def forward(ctx, i):
        result = i * torch.sigmoid(i)
        ctx.save_for_backward(i)
        return result

    @staticmethod
    def backward(ctx, grad_output):
        i = ctx.saved_variables[0]
        sigmoid_i = torch.sigmoid(i)
        return grad_output * (sigmoid_i * (1 + i * (1 - sigmoid_i)))


class MemoryEfficientSwish(nn.Module):
    def forward(self, x):
        return SwishImplementation.apply(x)


class Swish(nn.Module):
    def forward(self, x):
        return x * torch.sigmoid(x)


class Conv2dStaticSamePadding(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, bias=True, groups=1, dilation=1, **kwargs):
        super().__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size, stride=stride,
                              bias=bias, groups=groups)
        self.stride = self.conv.stride
        self.kernel_size = self.conv.kernel_size
        self.dilation = self.conv.dilation

        if isinstance(self.stride, int):
            self.stride = [self.stride] * 2
        elif len(self.stride) == 1:
            self.stride = [self.stride[0]] * 2

        if isinstance(self.kernel_size, int):
            self.kernel_size = [self.kernel_size] * 2
        elif len(self.kernel_size) == 1:
            self.kernel_size = [self.kernel_size[0]] * 2

    def forward(self, x):
        h, w = x.shape[-2:]

        # math.ceil=====>向上取整
        extra_h = (math.ceil(w / self.stride[1]) - 1) * self.stride[1] - w + self.kernel_size[1]
        extra_v = (math.ceil(h / self.stride[0]) - 1) * self.stride[0] - h + self.kernel_size[0]

        left = extra_h // 2
        right = extra_h - left
        top = extra_v // 2
        bottom = extra_v - top

        x = F.pad(x, [left, right, top, bottom])

        x = self.conv(x)
        return x


class MaxPool2dStaticSamePadding(nn.Module):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.pool = nn.MaxPool2d(*args, **kwargs)
        self.stride = self.pool.stride
        self.kernel_size = self.pool.kernel_size

        if isinstance(self.stride, int):
            self.stride = [self.stride] * 2
        elif len(self.stride) == 1:
            self.stride = [self.stride[0]] * 2

        if isinstance(self.kernel_size, int):
            self.kernel_size = [self.kernel_size] * 2
        elif len(self.kernel_size) == 1:
            self.kernel_size = [self.kernel_size[0]] * 2

    def forward(self, x):
        h, w = x.shape[-2:]

        extra_h = (math.ceil(w / self.stride[1]) - 1) * self.stride[1] - w + self.kernel_size[1]
        extra_v = (math.ceil(h / self.stride[0]) - 1) * self.stride[0] - h + self.kernel_size[0]

        left = extra_h // 2
        right = extra_h - left
        top = extra_v // 2
        bottom = extra_v - top

        x = F.pad(x, [left, right, top, bottom])

        x = self.pool(x)
        return x
