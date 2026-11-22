"""Generic chat agent runtime.

`RuntimeAgent(agent_kind)` loads the active profile, resolves model and
prompt, instantiates the LLM, applies the profile whitelist to a tool
list, and exposes a `run_stream(message, context)` async generator.

Call sites look like:

    agent = RuntimeAgent("custom")
    async for evt in agent.run_stream(message, {"session_id": ...}):
        ...

Tools still live in code (each agent's tool factory). The runtime
resolves which factory to invoke via `bindings`, then filters the
resulting LangChain tools by `profile.tool_python_names`.

Agent kinds with a streaming loop of their own subclass it.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from langchain_core.tools import BaseTool

from core.agent_profile import AgentProfileLoader, Profile, _default_environment
from core.prompt_resolver import PromptResolver
from core.skills import MAX_ALWAYS_ON, resolve_skills_for_request
from runtime.bindings import BindingResult, get_binding
from runtime.llm_factory import build_chat_model
from runtime.prompts._catalog import render_skill_catalog, render_tool_catalog
from runtime.prompts._tool_guard import prompt_mandated_unbound_tools
from runtime.tools._mcp_resolver import resolve_mcp_tools_for_profile
from runtime.tools.skills import READ_SKILL_TOOL_NAME, build_read_skill_tool

logger = logging.getLogger(__name__)

# Built by string concatenation and injected via literal replace/append —
# never format_map (see the {available_tools} comment in prepare()).
_SKILLS_PREAMBLE = (
    "## Skills\n"
    "Named playbooks with detailed instructions are available. When a user "
    "request matches a skill's purpose, call the `read_skill` tool with its "
    "slug BEFORE answering, then follow the returned instructions exactly. "
    "Do not guess at a skill's contents.\n"
)


class RuntimeAgent:
    """A profile-driven chat agent.

    Construct with the agent_kind; the runtime resolves the active
    profile via `AgentProfileLoader`. Pass a list of pre-built LangChain
    tools (from the agent's own factory) to `prepare()` and the runtime
    filters them down to the profile whitelist plus any binding-level
    filter.
    """

    def __init__(
        self,
        agent_kind: str,
        *,
        environment: Optional[str] = None,
        profile_slug: Optional[str] = None,
    ):
        self.agent_kind = agent_kind
        from core.profile_version_resolver import get_profile_pin, get_profile_version
        _pinned = get_profile_version()
        _pinned_profile = None if _pinned else get_profile_pin()
        if _pinned_profile:
            # The admin Test drawer / MCP chat pinned the profile being viewed
            # (a draft copy, a specialist). Same-kind only; None → not usable.
            _pinned_profile = AgentProfileLoader.load_pinned(_pinned_profile, agent_kind)
        if _pinned:
            # Eval/bake-off pinned an immutable snapshot for this request.
            self.profile: Profile = AgentProfileLoader.load_version(_pinned, environment)
        elif _pinned_profile is not None:
            self.profile = _pinned_profile
        elif profile_slug:
            # A surface asked for a SPECIFIC profile of this kind rather than
            # the kind's active one — today the duplex voice edge, which serves
            # calls from a voice-optimized specialist named by
            # voice_config.live.agent_profile_slug. An eval/bake-off version pin
            # still wins above: pinning an exact snapshot is a stronger, more
            # explicit request than "use the surface's usual profile".
            self.profile = self._load_profile_by_slug(agent_kind, profile_slug, environment)
        else:
            self.profile = AgentProfileLoader.load_active(agent_kind, environment)
        # True when THIS REQUEST chose the profile (an eval's version pin, the
        # admin Test drawer's profile pin) rather than the kind's serving
        # config. Readiness checks skip such turns: a pinned draft is not
        # what traffic gets (runtime.action_tools_check).
        self.profile_pinned: bool = bool(_pinned) or _pinned_profile is not None
        self.binding_fn = get_binding(agent_kind)
        # A profile pinned to the Automatic model row runs on whichever model
        # this install can use right now (core.model_resolver). Resolved per
        # request, on a COPY: the cached profile keeps the Automatic pin, so a
        # model added later is picked up without an edit or a cache flush.
        self.model_unresolved: bool = False
        from core.model_resolver import is_auto_model, resolve_auto_model
        if is_auto_model(getattr(self.profile, "model_main", None)):
            import dataclasses
            _resolved = resolve_auto_model(self.profile.model_main, self.profile.environment)
            self.model_unresolved = _resolved is None
            self.profile = dataclasses.replace(self.profile, model_main=_resolved)
        self.llm = self._build_llm(self.profile)
        logger.info(
            "RuntimeAgent ready: kind=%s profile=%s fallback=%s tools=%d",
            agent_kind, self.profile.profile_id, self.profile.is_fallback,
            len(self.profile.tool_python_names),
        )

    @staticmethod
    def _load_profile_by_slug(
        agent_kind: str, slug: str, environment: Optional[str]
    ) -> Profile:
        """Resolve `(agent_kind, slug)` for this env, falling back to active.

        FAIL-OPEN by design. The slug arrives from operator config
        (`voice_config.live.agent_profile_slug`), so a typo, a specialist that
        was never seeded in this environment, or a promote that hasn't run yet
        would otherwise take a whole surface down — a voice caller would get a
        provider-error close instead of the agent they had before the operator
        touched anything. Logging loudly and serving the kind's active profile
        keeps the field an additive, one-key rollback lever: clear it (or get
        it wrong) and behaviour is byte-identical to not setting it at all.

        `load_delegate` is the right resolver here rather than `load_active`:
        it keys on (kind, slug, environment) without filtering `status='active'`
        — a specialist is deliberately `status='specialist'` so it can never
        shadow the kind's active profile — and it caches like every other
        profile read.
        """
        try:
            return AgentProfileLoader.load_delegate(
                agent_kind, slug, environment or _default_environment()
            )
        except Exception as exc:
            logger.warning(
                "RuntimeAgent: profile slug %r for kind=%s did not resolve (%s); "
                "serving the kind's active profile instead",
                slug, agent_kind, exc,
            )
            return AgentProfileLoader.load_active(agent_kind, environment)

    def prepare(self, ctx: Dict[str, Any], all_tools: List[BaseTool]) -> Dict[str, Any]:
        """Apply binding hooks + tool whitelist for one request.

        Returns a dict that the agent-specific run_stream loop can plug
        into the LLM invocation:
          {system_prompt, tools, tools_by_name, metadata}
        """
        binding_result = self.binding_fn(self.profile, ctx) if self.binding_fn else BindingResult()

        # MCP path (spec 2026-07-05-portal-mcp-tools-design.md): the factory
        # list is a pool; profile MCP attachments decide what binds. HTTP
        # operator tools bind via bundle membership (see _bundle_tools).
        tools = resolve_mcp_tools_for_profile(self.profile, ctx, all_tools)
        if binding_result.tool_filter:
            tools = binding_result.tool_filter(tools)
        tools_by_name = {t.name: t for t in tools}

        # Snapshot-loaded profiles (non-dev) carry the resolved system
        # prompt directly. Passing it as PromptResolver.resolve's
        # `fallback` with no version id skips the DB roundtrip and reuses
        # the same variable-substitution path. In dev (no snapshot),
        # `fallback` is the file-system prompt the agent supplied.
        from core.prompt_resolver import override_slot

        snapshot_prompt = (self.profile.resolved_prompts or {}).get("system")
        system_prompt = PromptResolver.resolve(
            prompt_version_id=(
                None if snapshot_prompt is not None
                else (self.profile.prompt_version_ids or {}).get("system")
            ),
            fallback=snapshot_prompt if snapshot_prompt is not None
                else ctx.get("fallback_system_prompt", ""),
            variables=ctx.get("prompt_variables") or {},
            # Identify the prompt slot so the resolver can apply any
            # request-scoped X-WAJ-Prompt-Override (used by the admin
            # portal's prompt A/B feature). Falls through to the active
            # version when no override is set for this slot.
            agent_kind=self.agent_kind,
            prompt_key=override_slot(self.agent_kind, getattr(self.profile, "slug", None)),
        )
        if binding_result.system_addendum:
            system_prompt = system_prompt + "\n\n" + binding_result.system_addendum

        # Fill the generated tool catalog via a TARGETED literal replace, not
        # the resolver's `format_map`. Routing `{available_tools}` through
        # `format_map` would force that pass on for every prompt, and
        # `str.format_map` THROWS (invalid-format-spec) on the bare `{...}`
        # JSON example blocks some prompts carry — `_SafeDict` only covers missing *keys*, not malformed
        # specs. A literal `.replace` fills only this token and can't choke on
        # other `{...}` content; `{today}`/`{locale}`/etc stay literal for each
        # agent's downstream `.format()`.
        system_prompt = system_prompt.replace(
            "{available_tools}", render_tool_catalog(tools)
        )

        # Skills (spec 2026-07-22-agent-skills-design.md): progressive
        # disclosure. The catalog (name + description) is always in-prompt;
        # bodies load on demand via the synthetic read_skill tool; always_on
        # bodies are inlined (same raw-append posture as system_addendum —
        # skill text never passes through format_map). Tenant skills resolve
        # live by the request's authenticated business_id; platform skills
        # come from the profile (snapshot-frozen in prod).
        skill_entries = resolve_skills_for_request(self.profile, ctx)
        if skill_entries:
            section = _SKILLS_PREAMBLE + render_skill_catalog(skill_entries)
            if "{available_skills}" in system_prompt:
                system_prompt = system_prompt.replace("{available_skills}", section)
            else:
                system_prompt = system_prompt + "\n\n" + section
            always_on = [
                e for e in skill_entries
                if (e.get("skill") or {}).get("always_on")
                and (e.get("version") or {}).get("content")
            ]
            if len(always_on) > MAX_ALWAYS_ON:
                logger.warning(
                    "prepare: %d always_on skills exceed cap %d; extras load via read_skill",
                    len(always_on), MAX_ALWAYS_ON,
                )
            for entry in always_on[:MAX_ALWAYS_ON]:
                system_prompt += (
                    f"\n\n### Skill (always on): {entry['skill']['slug']}\n"
                    + entry["version"]["content"]
                )
            if READ_SKILL_TOOL_NAME in tools_by_name:
                # A pool/MCP tool claimed the name — never shadow operator
                # config with engine infrastructure. Bodies stay reachable
                # only for always_on skills this turn.
                logger.error(
                    "prepare: tool name collision on %r; skill bodies "
                    "unavailable via tool this turn", READ_SKILL_TOOL_NAME,
                )
            else:
                rs_tool = build_read_skill_tool(skill_entries)
                tools = list(tools) + [rs_tool]
                tools_by_name[READ_SKILL_TOOL_NAME] = rs_tool
        else:
            # Clean the token so a prompt authored with {available_skills}
            # never leaks the literal placeholder to the model.
            system_prompt = system_prompt.replace("{available_skills}", "")

        # Delegation (spec 2026-07-23-agent-context-optimization): each
        # delegatable downstream profile becomes an ask_<slug> tool. Depth-1:
        # a delegated run (ctx._delegation_depth >= 1) binds none. The
        # delegate resolves its tools from THIS SAME pool (all_tools), so
        # curation moves tools between parent and delegate purely in config.
        from runtime.delegation import build_delegate_tools

        for d_tool in build_delegate_tools(self.profile, ctx, all_tools):
            if d_tool.name in tools_by_name:
                logger.error(
                    "prepare: delegation tool name collision on %r; skipping bind",
                    d_tool.name,
                )
                continue
            tools = list(tools) + [d_tool]
            tools_by_name[d_tool.name] = d_tool

        # Runtime safety net: if the resolved prompt still tells the model to
        # call a tool the profile didn't bind, the model will call it and get
        # "Tool not found" — a silent apology/hallucination (#1032, twice).
        # The promote gate blocks this before it ships; log loudly here in case
        # one slips through (e.g. a live dev edit). Fail-open — never crash a turn.
        unbound = prompt_mandated_unbound_tools(system_prompt, all_tools, tools)
        if unbound:
            logger.error(
                "prepare: profile %s (kind=%s) system prompt mandates unbound "
                "tool(s) %s — the model will call them and get 'Tool not found'. "
                "Fix the profile's MCP tool binding or the prompt.",
                getattr(self.profile, "profile_id", "?"), self.agent_kind, unbound,
            )

        return {
            "system_prompt": system_prompt,
            "tools": tools,
            "tools_by_name": tools_by_name,
            "metadata": binding_result.metadata,
        }

    async def prefetch_voice_context(
        self, message: str, *, match_count: int
    ) -> Optional[str]:
        """Context to inject BEFORE the first LLM call of a voice turn, or None.

        Exists to collapse a two-call turn into one: an agent that can predict
        what its own retrieval tool would fetch can fetch it up front, so the
        model answers from context instead of spending a whole round trip
        deciding to search. Measured on a client demo VM 2026-08-19: KB
        questions cost 3568ms p50 as two calls.

        Default None — every agent that does not override this is unchanged,
        which is why this is a hook and not a required contract. Implementers
        MUST return None rather than raise, and MUST NOT block the event loop
        (wrap synchronous IO in asyncio.to_thread): the voice WS process runs
        its audio pumps on this loop.
        """
        return None

    def resolve_subagent_prompt(
        self,
        sub_kind: str,
        fallback: str,
        variables: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Resolve the system prompt of a sub-agent profile.

        Returns `fallback` (after variable substitution) when no sub-profile
        for `sub_kind` exists or the lookup fails — preserves today's
        file-system-prompt behaviour.
        """
        snapshot_prompt = None
        try:
            sub = AgentProfileLoader.load_active(sub_kind, self.profile.environment)
            if not sub.is_fallback:
                # Mirror prepare()'s main-prompt path: in non-dev the profile is
                # a self-contained snapshot — the admin-edited content lives in
                # sub.resolved_prompts, while prompt_version_ids points at a DEV
                # version id the prod/staging AI Supabase doesn't have. Prefer the
                # snapshot; only fall through to the version id in dev. Without
                # this, portal-edited sub-agent (title/suggestions) prompts were
                # silently inert in prod/staging (resolve → 'not found' → file
                # fallback).
                snapshot_prompt = (sub.resolved_prompts or {}).get("system")
                prompt_version_id = (
                    None if snapshot_prompt is not None
                    else (sub.prompt_version_ids or {}).get("system")
                )
            else:
                prompt_version_id = None
        except Exception:
            prompt_version_id = None
        return PromptResolver.resolve(
            prompt_version_id=prompt_version_id,
            fallback=snapshot_prompt if snapshot_prompt is not None else fallback,
            variables=variables,
            # Sub-agent prompts can also be A/B-tested — pin the sub-kind
            # so the override dict can target e.g. "<sub_kind>/system" while
            # the parent runs untouched.
            agent_kind=sub_kind,
            prompt_key="system",
        )

    def get_subagent_llm(self, sub_kind: str) -> Optional[Any]:
        """Resolve the LLM configured on a sub-agent profile.

        Used by the parent agent's title / suggestion / field-edit code
        paths so they hit the model the admin assigned to the sub-profile
        instead of the parent's main LLM. Returns `None` when no
        sub-profile is registered or its model_main can't be built — the
        caller should fall back to its own `self.llm`.
        """
        try:
            sub = AgentProfileLoader.load_active(sub_kind, self.profile.environment)
            if sub.is_fallback:
                return None
            return self._build_llm(sub)
        except Exception:
            return None

    @staticmethod
    def _build_llm(profile: Profile) -> Optional[Any]:
        """Build the main LLM for `profile`.

        An active request-scoped model override (set by the
        X-WAJ-Model-Override middleware, used by the eval comparison runner) is
        merged over `profile.model_main` so the request runs on a different
        model/gen-config WITHOUT mutating the cached profile.
        """
        model = profile.model_main
        from core.model_resolver import get_model_override

        override = get_model_override()
        if override:
            base = dict(model or {})
            base.update(override)
            # extra_config merges shallowly so e.g. location/top_p combine.
            if (model or {}).get("extra_config") or override.get("extra_config"):
                base["extra_config"] = {
                    **((model or {}).get("extra_config") or {}),
                    **(override.get("extra_config") or {}),
                }
            model = base
        return build_chat_model(model) if model else None

    async def run_stream(
        self, message: str, context: Dict[str, Any]
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Placeholder — per-agent migrations override this with their loop.

        Each migrated agent's `run_stream` calls `self.prepare(ctx, tools)`
        at the top, then runs its own streaming loop using the returned
        system_prompt + tools. Migrations land in follow-up PRs.
        """
        raise NotImplementedError(
            "RuntimeAgent.run_stream is overridden per migration. "
            "Use a subclass or call self.prepare(ctx, tools) directly."
        )
