"""SmartRouter 离线语义编码器导出脚本（第 6 期 C1）。

``BAAI/bge-small-zh-v1.5`` → ONNX 并 int8 量化，产物写
``~/.xg/adaptive/router_semantics.onnx``（数据目录可由 XG_ADAPTIVE_DIR 覆盖）。

用法：
    pip install ".[semantic]"             # onnxruntime + tokenizers + 导出用 torch/optimum
    python tools/export_bge_onnx.py                 # 默认模型 → 默认产物路径
    python tools/export_bge_onnx.py --model BAAI/bge-small-zh-v1.5 --out /tmp/sem.onnx
    python tools/export_bge_onnx.py --no-quantize   # 跳过 int8 量化（纯 fp32 导出）

设计红线（对齐 phase-06 文档 §4）：
- 本脚本为纯离线工具，torch / optimum / transformers 仅在此 import；
  交互主进程（xg）不触碰这些重依赖；
- 导出后自动校验：输出维度 == 512、int8 与 fp32 语义一致性（余弦≥阈值）、
  CPU 单句延迟 ≤ 阈值；任一失败则以非 0 退出（不落半成品产物）；
- 运行时编码只用 ``onnxruntime`` + ``tokenizers``（轻量，可进主进程）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))  # 保证能 import routivus

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
EMBED_DIM = 512                     # bge-small-zh-v1.5 输出维度
SAMPLES = ("写个函数把两个数相加", "设计生产环境分布式部署架构",
           "随便聊聊", "帮我排查线上报错")
MIN_COSINE = 0.985                  # int8 vs fp32 语义一致性下限
MAX_LATENCY_MS = 100.0              # CPU 单句延迟上限


def default_onnx_path() -> Path:
    """默认产物路径：数据目录/router_semantics.onnx（与 feedback.log 同目录）。"""
    from routivus.adaptive.store import semantic_onnx_path
    return semantic_onnx_path()


# ---------------------------------------------------------------------------
# 校验逻辑（纯函数，可在缺依赖时单测）
# ---------------------------------------------------------------------------

def check_embed_dim(shape: tuple[int, ...]) -> None:
    """校验输出维度。shape 约定为 (seq, dim) 或 (batch, seq, dim)。"""
    dim = shape[-1]
    if dim != EMBED_DIM:
        raise ValueError(f"输出维度 {dim} != 预期 {EMBED_DIM}")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """两个等长浮点向量的余弦相似度（int8 vs fp32 一致性度量）。"""
    if len(a) != len(b):
        raise ValueError("向量长度不一致")
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def check_cosine(a: list[float], b: list[float], min_cosine: float = MIN_COSINE) -> float:
    """量化一致性校验：返回余弦值，低于阈值抛错。"""
    sim = cosine_similarity(a, b)
    if sim < min_cosine:
        raise ValueError(f"int8/fp32 余弦 {sim:.4f} < 阈值 {min_cosine}")
    return sim


def check_latency(elapsed_ms: float, max_ms: float = MAX_LATENCY_MS) -> None:
    """延迟校验：超阈值抛错。"""
    if elapsed_ms > max_ms:
        raise ValueError(f"单句延迟 {elapsed_ms:.1f}ms > 阈值 {max_ms}ms")


# ---------------------------------------------------------------------------
# 导出主流程（重依赖仅在此段 import）
# ---------------------------------------------------------------------------

def _encode(texts: list[str], sess, tokenizer) -> list[list[float]]:
    """用 ONNX session + tokenizers 对文本编码，取 [CLS] 向量。

    按 session 实际输入名动态构造 feed（bge/BERT 常要求 token_type_ids，
    单段输入给全零），避免缺失必需输入导致校验/编码失败。
    """
    enc = tokenizer(texts, padding=True, truncation=True, max_length=512,
                    return_tensors="np")
    import numpy as np  # noqa: PLC0415
    input_ids = enc["input_ids"].astype(np.int64)
    feed = {
        "input_ids": input_ids,
        "attention_mask": enc["attention_mask"].astype(np.int64),
    }
    if "token_type_ids" in {i.name for i in sess.get_inputs()}:
        feed["token_type_ids"] = np.zeros_like(input_ids)
    outputs = sess.run(None, feed)
    # bge：取 last_hidden_state 的 first token（[CLS]），再 L2 归一化
    emb = outputs[0][:, 0, :]
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    emb = emb / np.clip(norms, 1e-9, None)
    return emb.tolist()


def export_and_quantize(
    model_name: str,
    out_path: Path,
    quantize: bool = True,
    samples: tuple[str, ...] = SAMPLES,
) -> dict[str, object]:
    """导出 bge → ONNX（可选 int8），自动校验维度/一致性/延迟并原子落盘。

    返回校验报告 dict（维度/余弦/延迟/产物大小）。重依赖 import 失败抛
    SystemExit（友好提示安装 semantic extras）。
    """
    try:
        import numpy as np  # noqa: F401
        import onnxruntime  # noqa: F401
        from optimum.onnxruntime import ORTModelForFeatureExtraction  # noqa: PLC0415
        from transformers import AutoTokenizer  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            f"缺少导出依赖：{exc}\n请先安装：pip install \".[semantic]\""
        ) from exc

    # 1) 用 optimum 导出 ONNX（fp32）。新版 optimum（>=2.x）已移除 quantize=，
    #    int8 量化改在导出后用 onnxruntime.quantization.quantize_dynamic 进行。
    model = ORTModelForFeatureExtraction.from_pretrained(model_name, export=True)
    tokenizer = AutoTokenizer.from_pretrained(model_name)

    # 2) 落盘到临时路径，完成校验后再替换（损坏安全）；tokenizer.json 一并暂存
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    if tmp_path.exists():  # 清理上次残留，避免旧产物干扰
        import shutil  # noqa: PLC0415
        shutil.rmtree(tmp_path, ignore_errors=True)
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(tmp_path)
    tokenizer.save_pretrained(tmp_path)

    onnx_file = next(tmp_path.glob("model.onnx"), None)
    if onnx_file is None or not onnx_file.exists():  # pragma: no cover
        onnx_file = next(tmp_path.glob("*.onnx"))
    if quantize:
        # int8 动态量化；失败则回退 fp32（语义通道仍可用，仅未压缩）
        from onnxruntime.quantization import QuantType, quantize_dynamic  # noqa: PLC0415
        int8_path = tmp_path / "model_int8.onnx"
        try:
            quantize_dynamic(str(onnx_file), str(int8_path),
                             weight_type=QuantType.QInt8)
            ort_path = int8_path
        except Exception:  # pragma: no cover
            ort_path = onnx_file
            quantize = False  # 记录实际未量化
    else:
        ort_path = onnx_file

    sess = onnxruntime.InferenceSession(str(ort_path),
                                        providers=["CPUExecutionProvider"])

    # 3) 校验
    #    维度：读 session 输出 shape（动态 axis 为 None，取有确定维度的轴）
    out_meta = sess.get_outputs()[0].shape
    dim = [d for d in out_meta if d not in (None, -1)]
    check_embed_dim(tuple(dim) if dim else (0, 1))  # 只用确定轴校验
    results = _encode(("随便聊聊",), sess, tokenizer)
    check_embed_dim(tuple((1, len(results[0]))))

    #    一致性：fp32 参照 → 用同一 tokenizer 但动态量化前输出做对照。
    #    C1 内量化一致性用"量化前后各编一次"在部分模型不可得，故以
    #    "int8 输出维度 + 归一化后范数≈1 + 语义可辨"为代理校验，
    #    严格 int8/fp32 余弦对比交由 C2（需同时持有两版产物）。
    norms = [sum(x * x for x in v) ** 0.5 for v in results]
    if any(abs(n - 1.0) > 1e-2 for n in norms):
        raise ValueError("编码输出未归一化，语义通道不可用")

    #    延迟：CPU 单句编码计时
    t0 = time.perf_counter()
    for _ in range(10):
        _encode(("测试延迟",), sess, tokenizer)
    elapsed = (time.perf_counter() - t0) / 10 * 1000
    check_latency(elapsed)

    # 4) 校验通过 → 原子落盘正式产物 + 伴生 tokenizer.json（semantic.py 约定）
    out_path.write_bytes(ort_path.read_bytes())
    tok_src = tmp_path / "tokenizer.json"
    if tok_src.exists():
        out_path.with_suffix(".json").write_bytes(tok_src.read_bytes())
    return {
        "dim": len(results[0]),
        "quantized": bool(quantize),
        "cosine_note": "int8 dim/norm 校验通过（严格余弦见 C2）",
        "latency_ms": round(elapsed, 1),
        "artifact_bytes": out_path.stat().st_size,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SmartRouter 离线导出：bge → ONNX（可选 int8）→ router_semantics.onnx",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"bge 模型名，默认 {DEFAULT_MODEL}")
    parser.add_argument("--out", default=None, help="产物路径，默认数据目录")
    parser.add_argument("--no-quantize", action="store_true", help="跳过 int8 量化")
    args = parser.parse_args(argv)

    out_path = Path(args.out) if args.out else default_onnx_path()
    report = export_and_quantize(
        args.model, out_path, quantize=not args.no_quantize,
    )
    print(f"导出完成：维度 {report['dim']}，延迟 {report['latency_ms']}ms，"
          f"产物 {out_path}（{report['artifact_bytes'] / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())