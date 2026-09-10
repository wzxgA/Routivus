# Third-Party Notices

本项目通过核心依赖与 optional extras（`pip install ".[semantic]"`、`pip install ".[terminal]"`）引入以下第三方依赖。
ML 精判与语义推理所依赖的 numpy / scikit-learn / LightGBM / joblib / onnxruntime /
tokenizers 已并入项目核心依赖，`pip install -e .` 即随装；torch/transformers/optimum 等
重依赖仅在离线导出脚本（`tools/export_bge_onnx.py`）中按需安装，不进入交互主进程。

运行时（交互主进程）仅用轻量依赖（onnxruntime + tokenizers + LightGBM 等）；
torch/transformers/optimum 这类重依赖只在离线导出脚本使用。

| 依赖                    | 版本要求   | 许可证          | 用途                          |
| --------------------- | ------ | ------------ | --------------------------- |
| numpy                 | >=1.26 | BSD-3-Clause | 数值数组与向量运算                   |
| scikit-learn          | >=1.4  | BSD-3-Clause | TF-IDF 特征向量化                |
| LightGBM              | >=4.0  | MIT          | 梯度提升树分类器（ML 精判）             |
| joblib                | >=1.3  | BSD-3-Clause | 训练产物（vectorizer + model）序列化 |
| onnxruntime           | >=1.17 | MIT          | 语义编码器推理引擎（CPU，无 CUDA）       |
| tokenizers            | >=0.15 | Apache-2.0   | 轻量 tokenizer                |
| optimum\[onnxruntime] | >=1.16 | Apache-2.0   | transformers → ONNX 导出（离线）  |
| transformers          | >=4.40 | Apache-2.0   | bge 模型加载（离线）                |
| torch                 | >=2.2  | BSD-3-Clause | 导出底座（离线）                    |
| safetensors           | >=0.4  | Apache-2.0   | 权重安全读取（离线）                  |
| pywinpty              | >=2.0  | MIT          | Windows ConPTY 伪终端后端（Web 终端） |

模型说明：`BAAI/bge-small-zh-v1.5`（bge-zh，Apache-2.0，BAAI 出品）共 33M 参数，
int8 量化产物约 30–40MB。许可证与论文见：
<https://github.com/FlagOpen/FlagEmbedding>

各许可证原文见官方仓库：

- numpy: <https://github.com/numpy/numpy/blob/main/LICENSE.txt>

- scikit-learn: <https://github.com/scikit-learn/scikit-learn/blob/main/COPYING>

- LightGBM: <https://github.com/microsoft/LightGBM/blob/master/LICENSE>

- joblib: <https://github.com/joblib/joblib/blob/main/LICENSE.txt>

- onnxruntime: <https://github.com/microsoft/onnxruntime/blob/main/LICENSE>

- tokenizers: <https://github.com/huggingface/tokenizers/blob/main/LICENSE>

- transformers: <https://github.com/huggingface/transformers/blob/main/LICENSE>

- optimum: <https://github.com/huggingface/optimum/blob/main/LICENSE>

- torch: <https://github.com/pytorch/pytorch/blob/main/LICENSE>

- safetensors: <https://github.com/huggingface/safetensors/blob/main/LICENSE>

- pywinpty: <https://github.com/andfoy/pywinpty/blob/main/LICENSE.txt>

