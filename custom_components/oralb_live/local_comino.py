"""Local-only validation backend for the Comino mouth-position model.

No model asset is part of the integration.  The preferred local validation
format is an exported NumPy archive.  It implements the model's two residual
bidirectional-GRU blocks directly with NumPy, avoiding the glibc-only LiteRT
wheel that cannot be installed on Home Assistant OS (musl).

The LiteRT loader remains as a development fallback so the NumPy path can be
compared against the original TFLite execution on supported workstations.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

MODEL_IDS = (
    "20200224-153124",
    "20200225-182337",
    "20200227-073830",
    "20200224-232918",
)


class _NumpyCominoModel:
    """One stateful Comino network executed with NumPy operations."""

    def __init__(self, np: Any, archive_path: Path) -> None:
        self._np = np
        with np.load(archive_path, allow_pickle=False) as archive:
            version = int(archive["format_version"])
            if version != 1:
                raise ValueError(f"unsupported Comino NumPy format {version}")
            self._initial_h1 = np.asarray(
                archive["initial_h1"], dtype=np.float32
            ).reshape(1, -1)
            self._initial_h2 = np.asarray(
                archive["initial_h2"], dtype=np.float32
            ).reshape(1, -1)
            self._layers = [
                {
                    "wx": self._dequantize(archive, f"gru_{index}_wx"),
                    "wh": self._dequantize(archive, f"gru_{index}_wh"),
                    "bx": np.asarray(archive[f"gru_{index}_bx"], dtype=np.float32),
                    "bh": np.asarray(archive[f"gru_{index}_bh"], dtype=np.float32),
                }
                for index in range(4)
            ]
            self._dense_weights = self._dequantize(archive, "dense_weights")
            self._dense_bias = np.asarray(archive["dense_bias"], dtype=np.float32)
        self.reset()

    def _dequantize(self, archive: Any, name: str) -> Any:
        np = self._np
        value = np.asarray(archive[name])
        if value.dtype.kind in {"i", "u"}:
            value = value.astype(np.float32) * np.float32(archive[f"{name}_scale"])
        return np.asarray(value, dtype=np.float32)

    def reset(self) -> None:
        self._h1 = self._initial_h1.copy()
        self._h2 = self._initial_h2.copy()

    def _gru(self, sequence: Any, hidden: Any, layer: dict[str, Any]) -> tuple:
        np = self._np
        output = []
        with np.errstate(
            over="ignore", under="ignore", divide="ignore", invalid="ignore"
        ):
            for sample in sequence:
                input_gates = sample[None, :] @ layer["wx"].T + layer["bx"]
                hidden_gates = hidden @ layer["wh"].T + layer["bh"]
                input_z, input_r, input_n = np.split(input_gates, 3, axis=1)
                hidden_z, hidden_r, hidden_n = np.split(hidden_gates, 3, axis=1)
                update = 1.0 / (1.0 + np.exp(-np.clip(input_z + hidden_z, -80.0, 80.0)))
                reset = 1.0 / (1.0 + np.exp(-np.clip(input_r + hidden_r, -80.0, 80.0)))
                candidate = np.tanh(input_n + reset * hidden_n)
                hidden = update * hidden + (1.0 - update) * candidate
                output.append(hidden[0].copy())
        return np.asarray(output, dtype=np.float32), hidden

    def predict(self, window: Any) -> Any:
        np = self._np
        inputs = np.asarray(window, dtype=np.float32)
        if inputs.shape != (26, 6):
            raise ValueError("Comino input must have shape [26, 6]")

        backward_1, _ = self._gru(
            inputs[::-1], np.zeros_like(self._h1), self._layers[0]
        )
        forward_1, self._h1 = self._gru(inputs, self._h1, self._layers[1])
        residual_1 = forward_1 + backward_1[::-1]

        backward_2, _ = self._gru(
            residual_1[::-1], np.zeros_like(self._h2), self._layers[2]
        )
        forward_2, self._h2 = self._gru(residual_1, self._h2, self._layers[3])
        residual_2 = residual_1 + forward_2 + backward_2[::-1]

        with np.errstate(
            over="ignore", under="ignore", divide="ignore", invalid="ignore"
        ):
            logits = residual_2 @ self._dense_weights.T + self._dense_bias
            logits -= logits.max(axis=1, keepdims=True)
            probabilities = np.exp(logits)
        probabilities /= probabilities.sum(axis=1, keepdims=True)
        return probabilities


class _LiteRTCominoModel:
    """Development fallback using Google's native LiteRT interpreter."""

    def __init__(self, np: Any, interpreter_type: Any, model: Path, state: Path):
        self._np = np
        self._interpreter = interpreter_type(model_path=str(model))
        self._interpreter.allocate_tensors()
        self._inputs = self._interpreter.get_input_details()
        self._outputs = self._interpreter.get_output_details()
        with state.open(encoding="utf-8") as state_file:
            initial = json.load(state_file)
        self._initial_h1 = np.asarray(initial["init_1_in"], dtype=np.float32).reshape(
            1, -1
        )
        self._initial_h2 = np.asarray(initial["init_2_in"], dtype=np.float32).reshape(
            1, -1
        )
        self.reset()

    def reset(self) -> None:
        self._h1 = self._initial_h1.copy()
        self._h2 = self._initial_h2.copy()

    def predict(self, window: Any) -> Any:
        np = self._np
        imu = np.asarray(window, dtype=np.float32)[None, :, :]
        if imu.shape != (1, 26, 6):
            raise ValueError("Comino input must have shape [26, 6]")
        for detail in self._inputs:
            name = detail["name"]
            if "imu_data" in name:
                value = imu
            elif "h0_1" in name:
                value = self._h1
            elif "h0_2" in name:
                value = self._h2
            else:  # pragma: no cover - guards against an unexpected asset
                raise RuntimeError(f"unexpected Comino input tensor {name}")
            self._interpreter.set_tensor(detail["index"], value)
        self._interpreter.invoke()

        probabilities = None
        for detail in self._outputs:
            value = self._interpreter.get_tensor(detail["index"])
            name = detail["name"]
            if tuple(value.shape) == (1, 26, 20):
                probabilities = value[0]
            elif name.endswith(":0"):
                self._h1 = value
            elif name.endswith(":1"):
                self._h2 = value
        if probabilities is None:  # pragma: no cover - invalid model asset
            raise RuntimeError("Comino model did not return [1, 26, 20]")
        return probabilities


class LocalCominoEnsemble:
    """Four-model, stateful validation ensemble matching the app pipeline."""

    def __init__(
        self, model_directory: Path, state_directory: Path | None = None
    ) -> None:
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("local Comino validation requires numpy") from exc

        self._np = np
        state_directory = state_directory or model_directory
        try:
            from ai_edge_litert.interpreter import Interpreter
        except ImportError:  # Home Assistant OS intentionally takes this path.
            Interpreter = None

        self._models = []
        for model_id in MODEL_IDS:
            numpy_path = model_directory / f"{model_id}.npz"
            model_path = model_directory / f"{model_id}.tflite"
            state_path = state_directory / f"{model_id}.json"
            if numpy_path.is_file():
                self._models.append(_NumpyCominoModel(np, numpy_path))
            elif (
                Interpreter is not None
                and model_path.is_file()
                and state_path.is_file()
            ):
                self._models.append(
                    _LiteRTCominoModel(
                        np, Interpreter, model_path=model_path, state=state_path
                    )
                )
            else:
                raise FileNotFoundError(
                    f"missing exported NumPy validation asset {numpy_path.name}"
                )
        self.reset()

    def reset(self) -> None:
        for model in self._models:
            model.reset()

    def predict(self, window: Sequence[Sequence[float]]) -> Sequence[Sequence[float]]:
        np = self._np
        combined = np.zeros((26, 20), dtype=np.float32)
        for model in self._models:
            combined += model.predict(window)
        return combined.tolist()
