# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import numpy as np
import pytest
import scipy

import tvm
import tvm.testing
from tvm import relax, relay
from tvm.contrib.cutlass.build import is_valid_for_cutlass_matmul
from tvm.relax.backend import get_patterns_with_prefix
from tvm.relax.backend.contrib.cutlass import partition_for_cutlass
from tvm.script import relax as R


@pytest.fixture(autouse=True)
def reset_seed():
    np.random.seed(0)


def get_relay_conv2d_bias_relu(
    d_shape, w_shape, data_dtype="float16", weight_dtype="float16", out_dtype="float16"
):
    data = relay.var("data", shape=d_shape, dtype=data_dtype)
    weight = relay.var("weight", shape=w_shape, dtype=weight_dtype)
    bias = relay.var("bias", shape=(1, 1, 1, w_shape[0]), dtype=out_dtype)
    return relay.nn.relu(
        relay.nn.conv2d(
            data=data,
            weight=weight,
            kernel_size=(3, 3),
            padding=(1, 1),
            data_layout="NHWC",
            kernel_layout="OHWI",
            out_dtype=out_dtype,
        )
        + bias
    )


def get_relay_conv2d_relu_x2(
    d_shape, w_shape, data_dtype="float16", weight_dtype="float16", out_dtype="float16"
):
    data = relay.var("data", shape=d_shape, dtype=data_dtype)
    weight1 = relay.var("weight1", shape=w_shape, dtype=weight_dtype)
    weight2 = relay.var("weight2", shape=w_shape, dtype=weight_dtype)

    conv1 = relay.nn.conv2d(
        data=data,
        weight=weight1,
        kernel_size=(3, 3),
        padding=(1, 1),
        data_layout="NHWC",
        kernel_layout="OHWI",
        out_dtype=out_dtype,
    )
    return relay.nn.conv2d(
        data=conv1,
        weight=weight2,
        kernel_size=(3, 3),
        padding=(1, 1),
        data_layout="NHWC",
        kernel_layout="OHWI",
        out_dtype=out_dtype,
    )


def get_relay_ref(relay_expr, *args):
    relay_mod = tvm.IRModule.from_expr(relay_expr)

    with tvm.transform.PassContext(opt_level=3):
        seq = tvm.transform.Sequential(
            [relay.transform.ConvertLayout({"nn.conv2d": ["NHWC", "HWIO"]})]
        )
        relay_mod = seq(relay_mod)

    return (
        relay.create_executor("graph", mod=relay_mod, device=tvm.cuda(0), target="cuda")
        .evaluate()(*args)
        .numpy()
    )


@tvm.script.ir_module
class Conv2dBiasReLU:
    @R.function
    def main(
        data: R.Tensor((16, 32, 32, 16), "float16"),
        weight: R.Tensor((32, 3, 3, 16), "float16"),
        bias: R.Tensor((1, 1, 1, 32), "float16"),
    ):
        with R.dataflow():
            conv1 = relax.op.nn.relu(
                relax.op.add(
                    relax.op.nn.conv2d(
                        data, weight, padding=(1, 1), data_layout="NHWC", kernel_layout="OHWI"
                    ),
                    bias,
                )
            )
            R.output(conv1)

        return conv1


@tvm.script.ir_module
class Conv2dx2:
    @R.function
    def main(
        data: R.Tensor((16, 32, 32, 16), "float16"),
        weight1: R.Tensor((16, 3, 3, 16), "float16"),
        weight2: R.Tensor((16, 3, 3, 16), "float16"),
    ):
        with R.dataflow():
            conv1 = relax.op.nn.conv2d(
                data, weight1, padding=(1, 1), data_layout="NHWC", kernel_layout="OHWI"
            )
            conv2 = relax.op.nn.conv2d(
                conv1, weight2, padding=(1, 1), data_layout="NHWC", kernel_layout="OHWI"
            )
            R.output(conv2)

        return conv2


has_cutlass = tvm.get_global_func("relax.ext.cutlass", True)

cutlass_enabled = pytest.mark.skipif(
    not has_cutlass,
    reason="CUTLASS not enabled.",
)

pytestmark = [cutlass_enabled]


def get_result_with_relax_cutlass_offload(mod, *args):
    patterns = [(entry.name, entry.pattern) for entry in get_patterns_with_prefix("cutlass")]
    assert len(patterns) != 0, "Cannot find cutlass patterns"

    seq = tvm.transform.Sequential(
        [
            relax.transform.FuseOpsByPattern(patterns, bind_constants=False, annotate_codegen=True),
            relax.transform.RunCodegen({"cutlass": {"sm": 80, "find_first_valid": True}}),
        ]
    )

    return vm["main"](*(tvm.nd.array(arg, dev) for arg in args)).numpy()


def test_conv2d_offload():
    data = np.random.randn(16, 32, 32, 16).astype("float16")
    weight = np.random.randn(32, 3, 3, 16).astype("float16")
    bias = np.random.randn(1, 1, 1, 32).astype("float16")

    out = get_result_with_relax_cutlass_offload(Conv2dBiasReLU, data, weight, bias)

    ref_relay_expr = get_relay_conv2d_bias_relu(data.shape, weight.shape)
    ref = get_relay_ref(ref_relay_expr, data, weight, bias)

    tvm.testing.assert_allclose(out, ref, rtol=1e-5, atol=1e-5)


def test_kernel_sharing():
    data_np = np.random.randn(16, 32, 32, 16).astype("float16")
    weight1_np = np.random.randn(16, 3, 3, 16).astype("float16")
    weight2_np = np.random.randn(16, 3, 3, 16).astype("float16")

    out = get_result_with_relax_cutlass_offload(Conv2dx2, data_np, weight1_np, weight2_np)

    relay_expr = get_relay_conv2d_relu_x2(data_np.shape, weight1_np.shape)
    ref = get_relay_ref(relay_expr, data_np, weight1_np, weight2_np)

    tvm.testing.assert_allclose(out, ref, rtol=1e-5, atol=1e-5)


def get_reference_matmul_result(x, y, bias=None, transposed_y=False, activation=None):
    if transposed_y:
        y = np.swapaxes(y, -2, -1)

    result = x @ y

    if bias is not None:
        result += bias

    if activation is None:
        pass
    elif activation is R.nn.relu:
        result = np.maximum(result, 0)
    elif activation is R.nn.gelu:
        result = 0.5 * result * (1 + scipy.special.erf(result / np.sqrt(2)))
    else:
        raise ValueError(f"Unknown activation function {activation}")

    return result


def get_relax_matmul_module(x, y, transposed_y=False, with_bias=False, activation=None):
    m, k = x.shape[-2:]
    if transposed_y:
        n = y.shape[-2]
    else:
        n = y.shape[-1]
    dtype = str(x.dtype)
    y_shape = y.shape

    from tvm.script.ir_builder import IRBuilder
    from tvm.script.ir_builder import relax as relax_builder

    with IRBuilder() as builder:
        with relax_builder.function():
            R.func_name("main")
            x = R.arg("x", R.Tensor(x.shape, dtype))
            y = R.arg("y", R.Tensor(y.shape, dtype))
            if with_bias:
                bias = R.arg("bias", R.Tensor((n,), dtype))

            with R.dataflow() as frame:
                if transposed_y:
                    axes = list(range(len(y_shape) - 2)) + [-1, -2]
                    y = R.emit(R.permute_dims(y, axes=axes))
                result = R.emit(R.matmul(x, y, out_dtype=dtype))
                if with_bias:
                    result = R.emit(result + bias)
                if activation is not None:
                    result = R.emit(activation(result))
                R.output(result)

            R.func_ret_value(frame.output_vars[0])

    func = builder.get()
    return tvm.IRModule({"main": func})


@pytest.mark.parametrize(
    "x_shape, y_shape, transpose_y",
    [
        # Regular
        ((32, 6), (6, 16), False),
        # Transposed
        ((4, 16), (16, 128), True),
        ((35, 8), (8, 8), True),
        # 3D x 3D
        ((6, 32, 8), (6, 8, 10), False),
        ((6, 32, 8), (6, 8, 10), True),
        # 3D x 2D
        ((6, 32, 8), (8, 10), False),
        ((10, 16, 8), (8, 10), True),
        # 2D x 3D
        ((32, 8), (10, 8, 10), False),
        ((32, 8), (10, 8, 10), True),
        # ND x 2D
        ((3, 6, 32, 8), (8, 10), False),
        # 2D x ND
        ((32, 8), (5, 3, 8, 10), False),
        # ND x ND
        ((5, 3, 32, 8), (5, 3, 8, 10), True),
        ((3, 2, 4, 16, 15), (1, 1, 15, 2), True),
        ((1, 1, 16, 15), (3, 2, 4, 15, 2), False),
    ],
)
@pytest.mark.parametrize(
    "with_bias, activation",
    [
        (True, None),
        (False, None),
        (True, R.nn.relu),
        (True, R.nn.gelu),
    ],
    ids=[
        "no_bias",
        "biased",
        "biased_relu",
        "biased_gelu",
    ],
)
@pytest.mark.parametrize(
    "dtype",
    [
        "float16",
    ],
)
def test_matmul_offload(
    x_shape,
    y_shape,
    transpose_y,
    with_bias,
    activation,
    dtype,
):
    x = np.random.randn(*x_shape).astype(dtype)
    y = np.random.randn(*y_shape).astype(dtype)

    if transpose_y:
        y = np.swapaxes(y, -2, -1)

    if with_bias:
        bias = np.random.randn(y_shape[-1]).astype(dtype)
        args = (x, y, bias)
    else:
        bias = None
        args = (x, y)

    ref = get_reference_matmul_result(x, y, bias, transposed_y=transpose_y, activation=activation)

    mod = get_relax_matmul_module(
        x, y, with_bias=with_bias, transposed_y=transpose_y, activation=activation
    )
    out = get_result_with_relax_cutlass_offload(mod, *args)

    tvm.testing.assert_allclose(out, ref, rtol=1e-2, atol=1e-3)


@pytest.mark.parametrize(
    "x_shape, y_shape, expected",
    [
        # Regular matmul
        ((3, 4), (4, 5), True),
        # Batch matmul without stretching
        ((3, 16, 15), (3, 15, 2), True),
        # Broadcast 2D to 3D
        ((3, 16, 15), (15, 2), True),
        ((16, 15), (3, 15, 2), True),
        # Broadcast one-length dimension
        ((1, 16, 15), (3, 15, 2), True),
        ((3, 16, 15), (1, 15, 2), True),
        ((1, 1, 16, 15), (3, 2, 4, 15, 2), True),
        # ND x ND
        ((3, 2, 4, 16, 15), (3, 2, 4, 15, 2), True),
        # ND x ND with one-length dimension
        ((1, 2, 4, 16, 15), (1, 2, 4, 15, 2), True),
        ((3, 2, 1, 16, 15), (3, 2, 1, 15, 2), True),
        # Extra one-length dimension doesn't block broadcasting
        ((3, 2, 1, 16, 15), (1, 1, 3, 2, 1, 15, 2), True),
        # Not broadcasting all dims. Cannot be computed by stride-based batch gemm
        ((3, 1, 1, 16, 15), (3, 2, 4, 15, 2), False),
        ((3, 2, 4, 16, 15), (2, 4, 15, 2), False),
        # Different shape
        ((3, 4, 16, 15), (3, 2, 15, 2), False),
    ],
)
def test_is_valid_for_cutlass_matmul(x_shape, y_shape, expected):
    assert is_valid_for_cutlass_matmul(x_shape, y_shape) == expected


@pytest.mark.parametrize(
    "x_shape, y_shape, transpose_y, dtype",
    [
        # Not broadcasting all dims. Cannot be computed by stride-based batch gemm
        ((3, 1, 1, 16, 15), (3, 2, 4, 15, 2), False, "float16"),
        ((1, 2, 1, 16, 15), (2, 1, 4, 15, 2), False, "float16"),
        ((3, 2, 4, 16, 15), (2, 4, 15, 2), True, "float16"),
        ((3, 16, 15), (2, 1, 3, 15, 2), True, "float16"),
        # Unsupported dtype
        ((4, 8), (8, 4), False, "float32"),
        ((5, 4, 8), (8, 4), True, "float32"),
    ],
)
def test_cutlass_partition_matmul_blocked(x_shape, y_shape, transpose_y, dtype):
    x = np.random.randn(*x_shape).astype(dtype)
    y = np.random.randn(*y_shape).astype(dtype)
    if transpose_y:
        y = np.swapaxes(y, -2, -1)

    mod = get_relax_matmul_module(x, y, with_bias=False, transposed_y=transpose_y)

    tvm.ir.assert_structural_equal(mod, partition_for_cutlass(mod))


if __name__ == "__main__":
    tvm.testing.main()
