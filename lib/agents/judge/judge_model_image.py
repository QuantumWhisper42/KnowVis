import asyncio
import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from itertools import batched
from pathlib import Path
from typing import Annotated, Any, Callable, TypeIs, cast

from diskcache import Cache
from pydantic import Discriminator, Field
from pydantic_ai import Agent, BinaryImage, RunContext
from pydantic_ai.usage import RunUsage
from pydantic_graph import BaseNode, End, Graph, GraphRunContext
from pydantic_graph.graph import GraphRunResult

from lib.models.prompt import (
    JudgedAdditionalVQAItem,
    JudgedVQAItem,
    JudgeModelImageAgentOutput,
    JudgeModelImagePipelineOutput,
    JudgmentType,
    PendingVQAChecklist,
    PendingAdditionalVQAItem,
    PendingVQAItem,
)
from lib.models.taxonomy import LabelTreeNode
from lib.tools.image_search.wikimedia_commons_image_search import DEFAULT_USER_AGENT
from lib.types import Language
from lib.utils.agent import build_judge_agent
from lib.utils.common import sync
from lib.utils.image import (
    create_rate_limited_async_httpx_get,
    load_image_from_url,
)
from lib.utils.image_source import ImageRef, ImageSource, build_image_source
from lib.utils.model_config import (
    DEFAULT_JUDGE_MODEL_CONFIG,
    load_judge_model_config,
)
from lib.utils.persistence import (
    SQLite3StatePersistence,
    get_persistance_db,
    run_persistent,
)
from lib.utils.template import build_message_template, get_template_file, load_raw_file

__all__ = ["judge"]

MAXIMUM_AGENT_RETRIES_EXCEEDED = "<MAXIMUM_AGENT_RETRIES_EXCEEDED>"

# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

image_cache = Cache(Path(tempfile.gettempdir()) / "judge_image_cache")


_httpx_get = create_rate_limited_async_httpx_get(user_agent=DEFAULT_USER_AGENT)


async def _load_image_from_url(image_url: str) -> BinaryImage:
    """Loads an image from the given URL."""

    result = image_cache.get(image_url)
    if result is not None:
        return cast(BinaryImage, result)

    result = await load_image_from_url(image_url, _httpx_get)

    image_cache.set(image_url, result, expire=60 * 60 * 24 * 7)

    return result


# ---------------------------------------------------------------------------
# Graph state & deps
# ---------------------------------------------------------------------------


def is_judged(val: JudgedVQAItem | PendingVQAItem) -> TypeIs[JudgedVQAItem]:
    return isinstance(val, JudgedVQAItem)


def is_judged_additional(
    val: JudgedAdditionalVQAItem | PendingAdditionalVQAItem,
) -> TypeIs[JudgedAdditionalVQAItem]:
    return isinstance(val, JudgedAdditionalVQAItem)


@dataclass
class JudgeState:
    """Mutable graph state threaded through every node."""

    category_path: list[LabelTreeNode]
    pending_vqa_checklist: PendingVQAChecklist
    image_refs: list[ImageRef] = field(default_factory=list)
    """Where the judged images live and what they hash to, never their bytes.

    Every snapshot of this state is serialized into the persistence db, so image bytes
    must stay out of it.
    """
    vqa_checklist: list[
        Annotated[
            PendingVQAItem | JudgedVQAItem,
            Field(discriminator=Discriminator("kind")),
        ]
    ] = field(default_factory=list)
    additional_vqa_checklist: list[
        Annotated[
            PendingAdditionalVQAItem | JudgedAdditionalVQAItem,
            Field(discriminator=Discriminator("kind")),
        ]
    ] = field(default_factory=list)
    judge_usage: RunUsage = field(default_factory=RunUsage)
    attempt: int = field(default=0)

    @classmethod
    def create(
        cls,
        category_path: list[LabelTreeNode],
        pending_vqa_checklist: PendingVQAChecklist,
        image_refs: list[ImageRef],
    ):
        return JudgeState(
            category_path=category_path,
            pending_vqa_checklist=pending_vqa_checklist,
            image_refs=image_refs,
            vqa_checklist=[
                PendingVQAItem(
                    id=i,
                    reasoning=MAXIMUM_AGENT_RETRIES_EXCEEDED,
                    judgment=JudgmentType.FAIL,
                    **vqa.model_dump(),
                )
                for i, vqa in enumerate(pending_vqa_checklist.vqa_checklist)
            ],
            additional_vqa_checklist=[
                PendingAdditionalVQAItem(
                    id=i,
                    reasoning=MAXIMUM_AGENT_RETRIES_EXCEEDED,
                    judgment=JudgmentType.FAIL,
                    **vqa.model_dump(),
                )
                for i, vqa in enumerate(pending_vqa_checklist.additional_vqa_checklist)
            ],
        )

    def __post_init__(self):
        if not self.vqa_checklist:
            raise ValueError("all_vqa_checklist cannot be empty")


class JudgeMode(str, Enum):
    BASELINE = "baseline"
    SKIP_LABEL = "skip_label"
    SKIP_KNOWLEDGE_HINT = "skip_knowledge_hint"
    SKIP_VISUAL_GROUNDING = "skip_visual_grounding"
    SKIP_ALL = "skip_all"
    GENERAL = "general"


@dataclass
class JudgeAgentDeps:
    language: Language
    mode: JudgeMode = field(default=JudgeMode.BASELINE)


@dataclass
class JudgeDeps:
    """Runtime dependencies injected into every node."""

    task_name: str
    judge_agent: Agent[JudgeAgentDeps, JudgeModelImageAgentOutput]
    judge_vqa_item_snippet: Callable[[dict[str, Any]], str]
    model_slug: str
    image_source: ImageSource
    batch_size: int = field(default=15)
    max_attempts_exceeding_normal_loops: int = field(default=5)
    judge_agent_deps: JudgeAgentDeps = field(
        default_factory=lambda: JudgeAgentDeps(language="en", mode=JudgeMode.BASELINE)
    )


# ---------------------------------------------------------------------------
# Graph node
# ---------------------------------------------------------------------------


@dataclass
class JudgeNode(
    BaseNode[
        JudgeState,
        JudgeDeps,
        JudgeModelImagePipelineOutput,
    ]
):
    """Judge node for VQA pipeline."""

    async def run(
        self, ctx: GraphRunContext[JudgeState, JudgeDeps]
    ) -> "JudgeNode | End[JudgeModelImagePipelineOutput]":
        state = ctx.state
        deps = ctx.deps

        pending_vqa_checklist = [
            vqa for vqa in state.vqa_checklist if not is_judged(vqa)
        ]

        pending_additional_vqa_checklist = [
            vqa
            for vqa in state.additional_vqa_checklist
            if not is_judged_additional(vqa)
        ]

        if not pending_vqa_checklist and not pending_additional_vqa_checklist:
            return End(
                JudgeModelImagePipelineOutput(
                    vqa_checklist=[
                        vqa for vqa in state.vqa_checklist if is_judged(vqa)
                    ],
                    additional_vqa_checklist=[
                        vqa
                        for vqa in state.additional_vqa_checklist
                        if is_judged_additional(vqa)
                    ],
                )
            )

        normal_loops = -(
            -len(state.vqa_checklist + state.additional_vqa_checklist)
            // deps.batch_size
        )

        if state.attempt >= normal_loops + deps.max_attempts_exceeding_normal_loops:
            return End(
                JudgeModelImagePipelineOutput(
                    vqa_checklist=[
                        vqa
                        if is_judged(vqa)
                        else JudgedVQAItem.model_validate(
                            vqa.model_dump(exclude={"kind"})
                            | {
                                "reason": MAXIMUM_AGENT_RETRIES_EXCEEDED,
                                "judgment": "FAIL",
                            }
                        )
                        for vqa in state.vqa_checklist
                    ],
                    additional_vqa_checklist=[
                        vqa
                        if is_judged_additional(vqa)
                        else JudgedAdditionalVQAItem.model_validate(
                            vqa.model_dump(exclude={"kind"})
                            | {
                                "reason": MAXIMUM_AGENT_RETRIES_EXCEEDED,
                                "judgment": "FAIL",
                            }
                        )
                        for vqa in state.additional_vqa_checklist
                    ],
                )
            )

        pending_vqa_checklist_batch = (
            next(batched(pending_vqa_checklist, deps.batch_size))
            if pending_vqa_checklist
            else []
        )

        pending_additional_vqa_checklist_batch = (
            next(batched(pending_additional_vqa_checklist, deps.batch_size))
            if pending_additional_vqa_checklist
            else []
        )

        model_dump_exclude = {"kind", "reasoning", "judgment", "weight"}

        if deps.judge_agent_deps.mode in {
            JudgeMode.SKIP_KNOWLEDGE_HINT,
            JudgeMode.SKIP_ALL,
        }:
            model_dump_exclude |= {"knowledge_hint"}

        if deps.judge_agent_deps.mode in {
            JudgeMode.SKIP_VISUAL_GROUNDING,
            JudgeMode.SKIP_ALL,
        }:
            model_dump_exclude |= {"visual_grounding"}

        template_ctx = {
            "category": state.category_path
            if deps.judge_agent_deps.mode
            not in {
                JudgeMode.SKIP_LABEL,
                JudgeMode.SKIP_ALL,
            }
            else [],
            "prompt": state.pending_vqa_checklist.prompt,
            "vqa_checklist": json.dumps(
                [
                    item.model_dump(exclude=model_dump_exclude)
                    | (
                        {}
                        if deps.judge_agent_deps.mode
                        in {
                            JudgeMode.SKIP_VISUAL_GROUNDING,
                            JudgeMode.SKIP_ALL,
                        }
                        else {
                            "visual_grounding": (
                                item.visual_grounding.title
                                or item.visual_grounding.source
                                or item.visual_grounding.url
                            )
                            if item.visual_grounding
                            else None
                        }
                    )
                    for item in pending_vqa_checklist_batch
                ],
                ensure_ascii=False,
                indent=2,
            ),
            "additional_vqa_checklist": json.dumps(
                [
                    item.model_dump(exclude=model_dump_exclude)
                    | (
                        {}
                        if deps.judge_agent_deps.mode
                        in {
                            JudgeMode.SKIP_VISUAL_GROUNDING,
                            JudgeMode.SKIP_ALL,
                        }
                        else {
                            "visual_grounding": (
                                item.visual_grounding.title
                                or item.visual_grounding.source
                                or item.visual_grounding.url
                            )
                            if item.visual_grounding
                            else None
                        }
                    )
                    for item in pending_additional_vqa_checklist_batch
                ],
                ensure_ascii=False,
                indent=2,
            ),
        }

        user_message = deps.judge_vqa_item_snippet(template_ctx)

        state.attempt += 1

        input_images = await deps.image_source.load(state.image_refs)

        if not input_images:
            raise ValueError(
                f"{state.pending_vqa_checklist.case_id} does not have input images"
            )

        result = await deps.judge_agent.run(
            user_prompt=(
                input_images
                + [user_message]
                + (
                    [
                        part
                        for item in pending_vqa_checklist_batch
                        if item.visual_grounding is not None
                        for part in (
                            f"<{item.visual_grounding.title or item.visual_grounding.source or item.visual_grounding.url}>:",
                            await _load_image_from_url(
                                item.visual_grounding.url or item.visual_grounding.source
                            ),
                        )
                    ]
                    if deps.judge_agent_deps.mode
                    not in {
                        JudgeMode.SKIP_VISUAL_GROUNDING,
                        JudgeMode.SKIP_ALL,
                    }
                    else []
                )
            ),
            deps=deps.judge_agent_deps,
        )

        state.judge_usage += result.usage()

        for vqa in result.output.vqa_checklist:
            if (
                vqa.id in range(len(state.vqa_checklist))
                and SequenceMatcher(
                    isjunk=None,
                    a=state.vqa_checklist[vqa.id].question,
                    b=vqa.question,
                ).ratio()
                > 0.5
            ):
                pending_vqa_item = state.vqa_checklist[vqa.id].model_dump(
                    exclude={"kind", "reasoning", "judgment"}
                )
                state.vqa_checklist[vqa.id] = JudgedVQAItem(
                    reasoning=vqa.reasoning,
                    judgment=vqa.judgment,
                    **pending_vqa_item,
                )

        for vqa in result.output.additional_vqa_checklist:
            if (
                vqa.id in range(len(state.additional_vqa_checklist))
                and SequenceMatcher(
                    isjunk=None,
                    a=state.additional_vqa_checklist[vqa.id].question,
                    b=vqa.question,
                ).ratio()
                > 0.5
            ):
                pending_additional_vqa_item = state.additional_vqa_checklist[
                    vqa.id
                ].model_dump(exclude={"kind", "reasoning", "judgment"})
                state.additional_vqa_checklist[vqa.id] = JudgedAdditionalVQAItem(
                    reasoning=vqa.reasoning,
                    judgment=vqa.judgment,
                    **pending_additional_vqa_item,
                )

        return JudgeNode()


# ---------------------------------------------------------------------------
# Graph definition
# ---------------------------------------------------------------------------

judge_graph = Graph(nodes=[JudgeNode], name="judge_prompt")


def image_refs_digest(image_refs: list[ImageRef]) -> str:
    """Fingerprints the judged images so replacing them cannot resume an old run.

    Only the content hashes go in, not the locations: moving the same images to another
    bucket or prefix keeps the existing judgment usable.
    """
    return hashlib.blake2b(
        ",".join(ref.md5 for ref in image_refs).encode("utf-8"), digest_size=16
    ).hexdigest()


async def judge_run_persistent(
    start_node: JudgeNode,
    state: JudgeState,
    deps: JudgeDeps,
) -> GraphRunResult[JudgeState, JudgeModelImagePipelineOutput]:
    persistence = SQLite3StatePersistence[JudgeState, JudgeModelImagePipelineOutput](
        db_path=get_persistance_db("judge"),
        run_id=f"{deps.task_name}#{deps.model_slug}#{state.pending_vqa_checklist.case_id}#{image_refs_digest(state.image_refs)}",
    )

    result = await run_persistent(
        graph=judge_graph,
        start_node=start_node,
        persistence=persistence,
        state=state,
        deps=deps,
        infer_name=True,
        reset_error_or_running_snapshots=True,
    )

    return result


@sync
async def judge(
    task_name: str,
    language: Language = "en",
    use_additional: bool = False,
    mode: JudgeMode = JudgeMode.BASELINE,
    model_config: str | None = None,
    image_s3_uri: str | None = None,
    s3_endpoint_url: str | None = None,
    no_sign_request: bool = False,
) -> None:
    import logfire
    from genai_prices import calc_price
    from rich import print as rprint

    logfire.configure(send_to_logfire=False)
    logfire.instrument_pydantic_ai()
    logfire.instrument_httpx(capture_all=True)

    from lib.models.prompt import JudgedVQAChecklist
    from lib.models.taxonomy import build_label_tree_node_path_from_str
    from lib.utils.datasets import (
        dump_judge_image_results,
        get_task_root,
        load_additional_vqa_checklist,
        load_taxonomy,
        load_vqa_checklist,
    )

    mode = JudgeMode(mode)

    if model_config is None:
        judge_model_config = DEFAULT_JUDGE_MODEL_CONFIG
    else:
        try:
            judge_model_config = load_judge_model_config(model_config)
        except ValueError as e:
            print(e)
            exit(1)

    rprint(
        f"Judging with {judge_model_config.provider} model {judge_model_config.model}",
        f"via {judge_model_config.entrypoint}"
        if judge_model_config.entrypoint
        else "(first-party endpoint)",
    )

    try:
        image_source = build_image_source(
            task_name=task_name,
            image_s3_uri=image_s3_uri,
            s3_endpoint_url=s3_endpoint_url,
            no_sign_request=no_sign_request,
        )
    except ValueError as e:
        print(e)
        exit(1)

    rprint(
        "Reading input images from",
        image_s3_uri or get_task_root(task_name) / "images",
    )

    T2I_TAXONOMY_ROOTS = load_taxonomy(task_name, language)

    pending_vqa_checklists = (
        load_additional_vqa_checklist(task_name=task_name, language=language)
        if use_additional
        else load_vqa_checklist(task_name=task_name, language=language)
    )

    judge_agent = build_judge_agent(
        config=judge_model_config,
        output_type=JudgeModelImageAgentOutput,
        system_prompt="",
        deps_type=JudgeAgentDeps,
    )

    @judge_agent.system_prompt
    async def judge_system_prompt(ctx: RunContext[JudgeAgentDeps]) -> str:
        return load_raw_file(
            get_template_file(
                f"judge/judge.{ctx.deps.mode.value}.system.md",
                language=ctx.deps.language,
            )
        )

    judge_deps = JudgeDeps(
        task_name=task_name,
        judge_agent=judge_agent,
        judge_vqa_item_snippet=build_message_template(
            get_template_file("judge/judge.user.jinja2.md", language=language)
        ),
        model_slug=judge_model_config.slug,
        image_source=image_source,
        judge_agent_deps=JudgeAgentDeps(language=language, mode=mode),
    )

    resolved_image_refs = await asyncio.gather(
        *[
            image_source.resolve(pending_vqa_checklist.case_id)
            for pending_vqa_checklist in pending_vqa_checklists
        ],
        return_exceptions=True,
    )

    resolve_exceptions: list[BaseException] = []
    judgeable_cases: list[tuple[PendingVQAChecklist, list[ImageRef]]] = []
    skipped_case_ids: list[str] = []

    for pending_vqa_checklist, image_refs in zip(
        pending_vqa_checklists, resolved_image_refs
    ):
        if isinstance(image_refs, BaseException):
            resolve_exceptions.append(image_refs)
        elif image_refs:
            judgeable_cases.append((pending_vqa_checklist, image_refs))
        else:
            skipped_case_ids.append(pending_vqa_checklist.case_id)

    graph_run_results = await asyncio.gather(
        *[
            judge_run_persistent(
                start_node=JudgeNode(),
                deps=judge_deps,
                state=JudgeState.create(
                    category_path=build_label_tree_node_path_from_str(
                        pending_vqa_checklist.category,
                        T2I_TAXONOMY_ROOTS,
                    ),
                    pending_vqa_checklist=pending_vqa_checklist,
                    image_refs=image_refs,
                ),
            )
            for pending_vqa_checklist, image_refs in judgeable_cases
        ],
        return_exceptions=True,
    )

    judge_usages = [
        graph_run_result.state.judge_usage
        for graph_run_result in graph_run_results
        if not isinstance(graph_run_result, BaseException)
    ]

    if len(judge_usages) > 0:
        total_judge_usage = sum(judge_usages, judge_usages[0])

        try:
            rprint(
                "Total judge cost: ",
                calc_price(
                    usage=total_judge_usage,
                    model_ref=judge_model_config.model,
                ),
            )
        except LookupError as e:
            rprint(f"Cannot price {judge_model_config.model}: {e}")

    rprint(
        "Exceptions:",
        resolve_exceptions
        + [result for result in graph_run_results if isinstance(result, BaseException)],
    )

    judged_vqa_checklists = [
        JudgedVQAChecklist(
            **graph_run_result.state.pending_vqa_checklist.model_dump(
                exclude={"vqa_checklist", "additional_vqa_checklist", "cot_prompt"}
            ),
            vqa_checklist=[
                vqa for vqa in graph_run_result.state.vqa_checklist if is_judged(vqa)
            ],
            additional_vqa_checklist=[
                vqa
                for vqa in graph_run_result.state.additional_vqa_checklist
                if is_judged_additional(vqa)
            ],
        )
        for graph_run_result in graph_run_results
        if not isinstance(graph_run_result, BaseException)
    ]

    dumped_judge_image_results_file = dump_judge_image_results(
        judged_vqa_checklists,
        task_name=task_name,
        language=language,
        model_name=judge_model_config.slug,
        image_source_slug=image_source.slug,
    )

    rprint(f"Judged {len(judged_vqa_checklists)}/{len(pending_vqa_checklists)} cases")

    if skipped_case_ids:
        rprint("Skipped cases without input images:", skipped_case_ids)

    rprint(
        "Model image judge results dumped to:",
        dumped_judge_image_results_file,
    )
