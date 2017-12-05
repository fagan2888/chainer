import chainer
from chainer.backends import cuda
from chainer import function_node
import chainer.functions
from chainer.utils import type_check


def _pair(x):
    if hasattr(x, '__getitem__'):
        return x
    return x, x


if cuda.available:
    cupy = cuda.cupy
    shift_gpu = cupy.ElementwiseKernel(
        'raw T x, int32 c, int32 h, int32 w,'
        'int32 kh, int32 kw,'
        'int32 dy, int32 dx',
        'T y',
        '''
           int b0 = i / (c * h * w);
           int rest = i % (c * h * w);
           int c0 = rest / (h * w);
           rest %= h * w;
           int out_row = rest / w;
           int out_col = rest % w;

           int n_groups = kh * kw;
           int group_size = c / n_groups;
           int group_idx = c0 / group_size;
           // Make sure that center group is last
           if (group_idx == (n_groups - 1) / 2) {
              group_idx = n_groups - 1;
           } else if (group_idx == n_groups - 1) {
              group_idx = (n_groups - 1) / 2;
           }

           int ky = (group_idx / kw) - kh / 2;
           int kx = (group_idx % kw) - kw / 2;
           if (group_idx >= n_groups) {
              ky = 0;
              kx = 0;
           }

           int in_row = -ky * dy + out_row;
           int in_col = -kx * dx + out_col;
           if (in_row >= 0 && in_row < h && in_col >= 0 && in_col < w) {
             y = x[b0 * c * h * w + c0 * h * w + in_row * w + in_col];
           } else {
             y = 0;
           }
        ''',
        'shift_gpu')


class ShiftFunction(function_node.FunctionNode):

    def __init__(self, ksize=3, dilate=1):
        super(ShiftFunction, self).__init__()
        self.kh, self.kw = _pair(ksize)
        assert self.kh % 2 == 1, 'kh must be odd'
        assert self.kw % 2 == 1, 'kw must be odd'
        self.dy, self.dx = _pair(dilate)

    def check_type_forward(self, in_types):
        n_in = in_types.size()
        type_check.expect(n_in == 1)

        x_type = in_types[0]
        type_check.expect(
            x_type.dtype.kind == 'f',
            x_type.ndim == 4,
            x_type.shape[1] >= self.kh * self.kw,
        )

    def forward_cpu(self, inputs):
        x = inputs[0]
        b, c, h, w = x.shape
        py = self.kh // 2 * abs(self.dy)
        px = self.kw // 2 * abs(self.dx)
        x = chainer.functions.pad(x, ((0, 0), (0, 0), (py, py), (px, px)),
                                  'constant')
        n_groups = self.kh * self.kw
        group_size = c // n_groups

        ret = []
        for i, group_idx in enumerate(range(n_groups)):
            # Make sure that center group is last
            if group_idx == (n_groups - 1) // 2:
                group_idx = n_groups - 1
            elif group_idx == (n_groups - 1):
                group_idx = (n_groups - 1) // 2

            ky = (group_idx // self.kw) - py // abs(self.dy)
            kx = (group_idx % self.kw) - px // abs(self.dx)

            hs = py + -ky * self.dy
            ws = px + -kx * self.dx
            he = hs + h
            we = ws + w
            cs = i * group_size
            ce = (i + 1) * group_size if i < n_groups - 1 else None
            ret.append(x[:, cs:ce, hs:he, ws:we])

        return chainer.functions.concat(ret).data,

    def forward_gpu(self, inputs):
        x = inputs[0]
        b, c, h, w = x.shape

        y = cupy.empty_like(x)
        shift_gpu(x, c, h, w, self.kh, self.kw, self.dy, self.dx, y)
        return y,

    def backward(self, indexes, grad_outputs):
        return shift(grad_outputs[0], ksize=(self.kh, self.kw),
                     dilate=(-self.dy, -self.dx)),


def shift(x, ksize=3, dilate=1):
    """Shift function.

    See: `Shift: A Zero FLOP, Zero Parameter Alternative to Spatial \
    Convolutions <https://arxiv.org/abs/1711.08141>`_

    Args:
        x (:class:`~chainer.Variable` or :class:`numpy.ndarray` or \
        :class:`cupy.ndarray`):
            Input variable of shape :math:`(n, c, h, w)`.
        ksize (int or pair of ints): Size of filters (a.k.a. kernels).
            ``ksize=k`` and ``ksize=(k, k)`` are equivalent.
        dilate (int or pair of ints): Dilation factor of filter applications.
            ``dilate=d`` and ``dilate=(d, d)`` are equivalent.

    Returns:
        ~chainer.Variable:
            Output variable of same shape as ``x``.
    """
    fnode = ShiftFunction(ksize, dilate)
    y, = fnode.apply((x,))
    return y
