import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__ = [
    "DEFAULT_JUDGE_MODEL_CONFIG",
    "JudgeModelConfig",
    "load_judge_model_config",
]


class JudgeModelConfig(BaseModel):
    """Judge model selection, plus optional routing through an internal LLM gateway."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["google", "openai"]
    model: str
    entrypoint: str | None = None
    """Gateway base URL, used verbatim.

    For `google` it must already contain the full resource prefix, e.g.
    `https://<gateway>/v1/projects/<project>/locations/<location>`, because google-genai
    only appends `publishers/google/models/<model>:generateContent` to it.
    For `openai` it must be the part before `/chat/completions`, e.g. `https://<gateway>/v1`.
    """
    headers: dict[str, str] = Field(default_factory=dict)
    reasoning_effort: Literal["low", "medium", "high"] | None = "high"
    """Only used by `openai`; the `google` path uses `ThinkingLevel.HIGH`.

    Set to `null` to omit the parameter entirely, which some deployments require.
    """
    output_mode: Literal["tool", "native", "prompted"] | None = None
    """How structured output is requested. Defaults to `tool` for `google`, `native` for `openai`.

    Some chat/completions deployments reject `reasoning_effort` as soon as the request
    carries function tools, and `tool` mode sends the output schema as one. `native`
    asks for a JSON schema response instead, `prompted` only asks for a JSON object.
    """

    @property
    def resolved_output_mode(self) -> Literal["tool", "native", "prompted"]:
        if self.output_mode is not None:
            return self.output_mode
        return "tool" if self.provider == "google" else "native"

    @model_validator(mode="after")
    def _reject_googleapis_entrypoint(self) -> "JudgeModelConfig":
        # google-genai keeps project/location (and therefore injects an ADC bearer token)
        # whenever the custom base URL ends with `.googleapis.com`.
        if (
            self.provider == "google"
            and self.entrypoint is not None
            and self.entrypoint.endswith(".googleapis.com")
        ):
            raise ValueError(
                "entrypoint must not end with '.googleapis.com', otherwise google-genai "
                "falls back to Application Default Credentials instead of the gateway headers"
            )
        return self

    @property
    def slug(self) -> str:
        """Model name reduced to characters that are safe in filenames and run ids."""
        return re.sub(r"[^0-9A-Za-z._-]+", "-", self.model).strip("-")


DEFAULT_JUDGE_MODEL_CONFIG = JudgeModelConfig(
    provider="google", model="gemini-3.1-pro-preview"
)


def load_judge_model_config(path: str | Path) -> JudgeModelConfig:
    config_path = Path(path)

    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Cannot read judge model config {config_path}: {e}") from e

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{config_path} is not valid JSON: {e}") from e

    try:
        return JudgeModelConfig.model_validate(payload)
    except ValidationError as e:
        raise ValueError(f"Invalid judge model config {config_path}:\n{e}") from e
