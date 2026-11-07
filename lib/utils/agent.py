import os
from types import NoneType
from typing import Mapping, Sequence

from google.auth.credentials import AnonymousCredentials
from google.genai import Client
from google.genai.types import (
    HttpOptions,
    HarmBlockThreshold,
    HarmCategory,
    ResourceScope,
    SafetySettingDict,
    ThinkingConfigDict,
    ThinkingLevel,
)
from httpx import (
    AsyncClient,
    HTTPStatusError,
    Response,
    TimeoutException,
    ConnectError,
    ReadError,
)
from openai import AsyncOpenAI
from pydantic_ai import Agent, WebFetchTool, WebSearchTool
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai import ConcurrencyLimiter
from pydantic_ai.builtin_tools import AbstractBuiltinTool
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.output import (
    NativeOutput,
    OutputDataT,
    OutputSpec,
    PromptedOutput,
    ToolOutput,
)
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig, wait_retry_after
from pydantic_ai.tools import AgentDepsT, BuiltinToolFunc, Tool, ToolFuncEither
from pydantic_ai.toolsets import AbstractToolset, ToolsetFunc
from tenacity import retry_if_exception_type, stop_after_attempt, wait_exponential

from lib.utils.google_model_fix import MyGoogleModel
from lib.utils.model_config import JudgeModelConfig
import httpx

# Not public API, but it is the exact helper httpx itself uses to turn `*_proxy` and
# `NO_PROXY` into a mount map, including the curl-compatible bypass rules. Reproducing
# those rules by hand is what makes hand-rolled proxy setups leak internal traffic.
from httpx._utils import get_environment_proxies


NO_RETRY = os.environ.get("JUDGE_NO_RETRY") == "1"
"""Surface the very first response a request gets, unretried.

Set `JUDGE_NO_RETRY=1` when debugging a gateway: every layer that would otherwise
swallow that response and try again is turned off, every timeout is lifted so neither a
slow upload of a large body nor a slow generation fails on its own, and each response is
reported with both its declared and its actual length.

Lifting the timeouts is the part that decides whether a slow model answers at all, rather
than only how its failures get reported: a request then waits indefinitely. So a run that
succeeds only under this flag is evidence about `GATEWAY_TIMEOUT`, not about the retries.
"""


GATEWAY_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=120.0, pool=60.0)
"""Per-phase budget for one gateway request.

Only the chat/completions path is bound by this. The OpenAI SDK adopts the client's
timeout as its own whenever it differs from httpx's default, while google-genai passes an
explicit per-request timeout -- `None` unless `HttpOptions.timeout` is set -- which
overrides whatever the client was built with.

The phases need budgets orders of magnitude apart, which is why one scalar cannot serve
them all:

- `read` has to cover an entire generation, because chat/completions sends nothing until
  the answer is complete. A reasoning model working through a batch of checklist items
  alongside several images holds this phase open for minutes.
- `write` has to cover uploading that request body, which is megabytes of base64 images.
- `connect` is a handshake with an internal gateway: either quick or genuinely
  unreachable. Sharing the generation budget here would mean waiting ten minutes to find
  that out.
- `pool` should never bind, since `openai_limiter` keeps fewer requests in flight than
  httpx's connection limit. A finite value turns a leaked connection into an error rather
  than a permanent hang.
"""


async def _report_raw_response(response: Response) -> None:
    # Event hooks run before httpx reads the body; `aread` here is what makes `content`
    # available, and the later read inside `send` reuses it.
    body = await response.aread()
    print(
        f"[raw] {response.status_code} {response.request.method} {response.request.url}\n"
        f"      sent={response.request.headers.get('content-length', '?')}B"
        f" content-length={response.headers.get('content-length', '?')}B"
        f" received={len(body)}B"
        f" type={response.headers.get('content-type', '?')}\n"
        f"      tail={body[-200:]!r}",
        flush=True,
    )


def create_rate_limit_network_resilient_client(
    raise_for_status: bool = True, max_attempts: int = 10
):
    """Create a client that respects Retry-After headers from rate limiting responses.

    `max_attempts` is this transport's own budget, and it multiplies with any retry loop
    the SDK layered on top runs. It therefore belongs to whichever of the two can actually
    see the failure being retried.
    """

    def build_transport(proxy_url: str | None) -> AsyncTenacityTransport:
        return AsyncTenacityTransport(
            wrapped=httpx.AsyncHTTPTransport(
                proxy=(httpx.Proxy(proxy_url) if proxy_url else None)
            ),
            config=RetryConfig(
                retry=retry_if_exception_type(
                    (
                        HTTPStatusError,
                        TimeoutException,
                        ConnectError,
                        ReadError,
                    )
                ),
                wait=wait_retry_after(
                    fallback_strategy=wait_exponential(multiplier=1, max=60),
                    max_wait=300,  # Don't wait more than 5 minutes
                ),
                stop=stop_after_attempt(1 if NO_RETRY else max_attempts),
                reraise=True,
            ),
            validate_response=(
                (lambda r: r.raise_for_status())  # Raises HTTPStatusError for 4xx/5xx
                if raise_for_status
                else None
            ),
        )

    # httpx only reads proxies from the environment when it builds the transport itself,
    # which it refuses to do once `transport=` is passed. So the environment's mount map
    # is rebuilt here around the retrying transport: a `None` mount means "use the
    # client's default transport", which is the direct, proxy-less one, and that is how
    # the hosts listed in NO_PROXY end up bypassing the proxy.
    proxy_transports: dict[str, AsyncTenacityTransport] = {}
    mounts: dict[str, httpx.AsyncBaseTransport | None] = {}
    for pattern, proxy_url in get_environment_proxies().items():
        if proxy_url is None:
            mounts[pattern] = None
        else:
            mounts[pattern] = proxy_transports.setdefault(
                proxy_url, build_transport(proxy_url)
            )

    return AsyncClient(
        transport=build_transport(None),
        mounts=mounts,
        timeout=None if NO_RETRY else GATEWAY_TIMEOUT,
        event_hooks={"response": [_report_raw_response]} if NO_RETRY else {},
    )


# google-genai runs no retry loop of its own, which leaves this transport as the only
# layer that ever sees a 429 or a 503 and can wait out its Retry-After.
rate_limit_network_resilient_client = create_rate_limit_network_resilient_client()

# The OpenAI SDK parses 4xx/5xx bodies into its own error types and does its own
# Retry-After handling, so this transport must hand the response back untouched. Doing so
# also hides every status code from it, leaving network errors as the only thing it could
# retry -- and the SDK retries those already, with the same coverage. Hence a single
# attempt here: the two budgets would otherwise multiply into dozens of uploads of the
# same multi-megabyte body.
network_resilient_client = create_rate_limit_network_resilient_client(
    raise_for_status=False, max_attempts=1
)


shared_limiter = ConcurrencyLimiter(max_running=30, name="gemini-pool")

openai_limiter = ConcurrencyLimiter(max_running=30, name="openai-pool")


def _build_google_client(
    entrypoint: str | None, headers: Mapping[str, str] | None
) -> Client:
    if entrypoint is None:
        return Client(
            vertexai=True,
            http_options=HttpOptions(
                api_version="v1",
                httpx_async_client=rate_limit_network_resilient_client,
            ),
        )

    return Client(
        vertexai=True,
        # Never pass project/location here: google-genai injects an ADC bearer token on
        # every request as soon as either is set on the client. Leaving them unset makes
        # it clear them from the environment too, so no Google OAuth token is fetched.
        credentials=AnonymousCredentials(),
        http_options=HttpOptions(
            api_version="v1",
            base_url=entrypoint,
            # Under COLLECTION scope google-genai appends only the resource path, so
            # `entrypoint` has to carry the `/v1/projects/.../locations/...` prefix itself.
            base_url_resource_scope=ResourceScope.COLLECTION,
            headers=dict(headers or {}),
            httpx_async_client=rate_limit_network_resilient_client,
        ),
    )


def _build_openai_provider(
    entrypoint: str | None, headers: Mapping[str, str] | None
) -> OpenAIProvider:
    if entrypoint is None:
        return OpenAIProvider(http_client=network_resilient_client)

    return OpenAIProvider(
        openai_client=AsyncOpenAI(
            base_url=entrypoint,
            # The SDK requires a non-empty key; `default_headers` takes precedence over the
            # `Authorization: Bearer <api_key>` it would otherwise send.
            api_key="unused-gateway-authenticates-via-headers",
            default_headers=dict(headers or {}),
            http_client=network_resilient_client,
            max_retries=0 if NO_RETRY else 5,
        )
    )


def build_gemini_agent(
    model_name: str,
    system_prompt: str | Sequence[str],
    output_type: OutputSpec[OutputDataT] = str,
    deps_type: type[AgentDepsT] = NoneType,
    builtin_tools: Sequence[AbstractBuiltinTool | BuiltinToolFunc[AgentDepsT]] = [
        WebSearchTool(),
        WebFetchTool(),
    ],
    tools: Sequence[Tool[AgentDepsT] | ToolFuncEither[AgentDepsT, ...]] = (),
    toolsets: Sequence[AbstractToolset[AgentDepsT] | ToolsetFunc[AgentDepsT]]
    | None = None,
    thinking_level: ThinkingLevel = ThinkingLevel.HIGH,
    entrypoint: str | None = None,
    headers: Mapping[str, str] | None = None,
):
    return Agent(
        model=ConcurrencyLimitedModel(
            MyGoogleModel(
                model_name=model_name,
                provider=GoogleProvider(
                    client=_build_google_client(entrypoint, headers)
                ),
            ),
            limiter=shared_limiter,
        ),
        deps_type=deps_type,
        output_type=output_type,
        system_prompt=system_prompt,
        model_settings=GoogleModelSettings(
            google_safety_settings=[
                SafetySettingDict(
                    category=HarmCategory.HARM_CATEGORY_HARASSMENT,
                    threshold=HarmBlockThreshold.OFF,
                ),
                SafetySettingDict(
                    category=HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                    threshold=HarmBlockThreshold.OFF,
                ),
                SafetySettingDict(
                    category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                    threshold=HarmBlockThreshold.OFF,
                ),
                SafetySettingDict(
                    category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                    threshold=HarmBlockThreshold.OFF,
                ),
            ],
            google_thinking_config=ThinkingConfigDict(thinking_level=thinking_level),
        ),
        builtin_tools=builtin_tools,
        tools=tools,
        toolsets=toolsets,
    )


def build_openai_agent(
    model_name: str,
    system_prompt: str | Sequence[str],
    output_type: OutputSpec[OutputDataT] = str,
    deps_type: type[AgentDepsT] = NoneType,
    tools: Sequence[Tool[AgentDepsT] | ToolFuncEither[AgentDepsT, ...]] = (),
    toolsets: Sequence[AbstractToolset[AgentDepsT] | ToolsetFunc[AgentDepsT]]
    | None = None,
    reasoning_effort: str | None = "high",
    entrypoint: str | None = None,
    headers: Mapping[str, str] | None = None,
):
    """Build an agent backed by the chat/completions API.

    No built-in tools are registered: the deployments behind the gateway have no web
    search or fetch capability, and `OpenAIChatModel` rejects `WebSearchTool` outright.
    """
    return Agent(
        model=ConcurrencyLimitedModel(
            OpenAIChatModel(
                model_name,
                provider=_build_openai_provider(entrypoint, headers),
            ),
            limiter=openai_limiter,
        ),
        deps_type=deps_type,
        output_type=output_type,
        system_prompt=system_prompt,
        model_settings=(
            OpenAIChatModelSettings(
                openai_reasoning_effort=reasoning_effort,  # pyright: ignore[reportArgumentType]
            )
            if reasoning_effort is not None
            else None
        ),
        builtin_tools=[],
        tools=tools,
        toolsets=toolsets,
        # Otherwise a malformed response is answered with a repair prompt rather than
        # raised, and the body that caused it never reaches the caller.
        retries=0 if NO_RETRY else 1,
    )


def _wrap_judge_output(
    config: JudgeModelConfig, output_type: type[OutputDataT]
) -> OutputSpec[OutputDataT]:
    match config.resolved_output_mode:
        case "tool":
            return ToolOutput(output_type)
        case "native":
            return NativeOutput(output_type)
        case "prompted":
            return PromptedOutput(output_type)


def build_judge_agent(
    config: JudgeModelConfig,
    system_prompt: str | Sequence[str],
    output_type: type[OutputDataT],
    deps_type: type[AgentDepsT] = NoneType,
    tools: Sequence[Tool[AgentDepsT] | ToolFuncEither[AgentDepsT, ...]] = (),
    toolsets: Sequence[AbstractToolset[AgentDepsT] | ToolsetFunc[AgentDepsT]]
    | None = None,
):
    output_spec = _wrap_judge_output(config, output_type)

    match config.provider:
        case "google":
            return build_gemini_agent(
                model_name=config.model,
                system_prompt=system_prompt,
                output_type=output_spec,
                deps_type=deps_type,
                tools=tools,
                toolsets=toolsets,
                entrypoint=config.entrypoint,
                headers=config.headers,
            )
        case "openai":
            return build_openai_agent(
                model_name=config.model,
                system_prompt=system_prompt,
                output_type=output_spec,
                deps_type=deps_type,
                tools=tools,
                toolsets=toolsets,
                reasoning_effort=config.reasoning_effort,
                entrypoint=config.entrypoint,
                headers=config.headers,
            )
