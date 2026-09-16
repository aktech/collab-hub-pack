"""The result envelope: what a Cog's usage entry point returns (version 1).

The shape is ``docs/cog-execution/result-envelope.md``. This module is the
hub's reading of it — the one place the seam's return value is parsed — so the
engine, the HTTP worker client and the in-memory executor all agree on what a
worker answered. Consumers detect the version through ``envelope`` and ignore
fields they do not know, as the document's versioning rule requires.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

ENVELOPE_VERSION = 1

# The error codes the envelope document defines. ``code`` is kept verbatim on
# the Track's ``failed`` event, so a client can act on it without parsing text.
ERROR_CODES = frozenset(
    {"binding-invalid", "invalid-input", "model-unavailable", "model-call-failed", "model-response-malformed"}
)

# Over HTTP, the status a worker uses for each code (result-envelope.md, "error").
STATUS_FOR_CODE: Mapping[str, int] = {
    "invalid-input": 422,
    "model-call-failed": 502,
    "model-response-malformed": 502,
    "model-unavailable": 503,
    "binding-invalid": 503,
}

# ...and the code the client assumes when one of those statuses arrives without
# a parseable envelope body (a proxy's 502 page, a crashed worker's 503).
CODE_FOR_STATUS: Mapping[int, str] = {422: "invalid-input", 502: "model-call-failed", 503: "model-unavailable"}

SEVERITIES = ("error", "warn")


class EnvelopeInvalid(ValueError):
    """A worker's return is not a version-1 result envelope."""


@dataclass(frozen=True, slots=True)
class Problem:
    """One self-reported contract-check finding: input to Guards, never a verdict."""

    check: str
    detail: str
    severity: str = "error"


@dataclass(frozen=True, slots=True)
class EnvelopeError:
    """Why ``ok`` is false: a code from ``ERROR_CODES`` and a human sentence."""

    code: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ResultEnvelope:
    """A parsed envelope. ``usage`` is kept as reported; the engine validates it
    against the run's budget, so a malformed report fails the run as unknown
    spending rather than being silently dropped here."""

    ok: bool
    payload: Any = None
    problems: tuple[Problem, ...] = ()
    error: EnvelopeError | None = None
    binding: Mapping[str, Any] | None = None
    usage: Any = None
    raw: str | None = None
    cog: Mapping[str, Any] | None = None
    task: str | None = None
    timing: Mapping[str, Any] | None = None
    envelope: int = field(default=ENVELOPE_VERSION)

    @classmethod
    def success(
        cls,
        payload: Any = None,
        *,
        usage: Any = None,
        problems: tuple[Problem, ...] | list[Problem] = (),
        binding: Mapping[str, Any] | None = None,
        raw: str | None = None,
        cog: Mapping[str, Any] | None = None,
        task: str | None = None,
        timing: Mapping[str, Any] | None = None,
    ) -> ResultEnvelope:
        return cls(
            ok=True, payload=payload, problems=tuple(problems), binding=binding, usage=usage,
            raw=raw, cog=cog, task=task, timing=timing,
        )

    @classmethod
    def failure(
        cls,
        code: str,
        detail: str = "",
        *,
        usage: Any = None,
        problems: tuple[Problem, ...] | list[Problem] = (),
        binding: Mapping[str, Any] | None = None,
        raw: str | None = None,
        cog: Mapping[str, Any] | None = None,
        task: str | None = None,
        timing: Mapping[str, Any] | None = None,
    ) -> ResultEnvelope:
        return cls(
            ok=False, payload=None, problems=tuple(problems), error=EnvelopeError(code, detail), binding=binding,
            usage=usage, raw=raw, cog=cog, task=task, timing=timing,
        )

    @classmethod
    def parse(cls, data: Any) -> ResultEnvelope:
        """Read a version-1 envelope from decoded JSON, ignoring unknown fields.

        Raises ``EnvelopeInvalid`` for anything that is not an envelope: a
        missing or unsupported ``envelope`` version, a non-boolean ``ok``, an
        ``ok: false`` with no ``error`` (an unexplained failure) or an
        ``ok: true`` with one (a contradiction), and malformed ``problems``.
        ``usage`` is passed through for the engine's budget rules.
        """
        if not isinstance(data, Mapping):
            raise EnvelopeInvalid("a result envelope is a JSON object")
        version = data.get("envelope")
        if version is None:
            raise EnvelopeInvalid("missing envelope version")
        if type(version) is not int or version != ENVELOPE_VERSION:
            raise EnvelopeInvalid(f"unsupported envelope version {version!r}; this hub reads {ENVELOPE_VERSION}")
        ok = data.get("ok")
        if type(ok) is not bool:
            raise EnvelopeInvalid("ok must be a boolean")
        error = _parse_error(data.get("error"))
        if not ok and error is None:
            raise EnvelopeInvalid("ok: false requires error {code, detail}")
        if ok and error is not None:
            raise EnvelopeInvalid("ok: true cannot carry an error")
        return cls(
            ok=ok,
            payload=data.get("payload"),
            problems=_parse_problems(data.get("problems")),
            error=error,
            binding=_optional_mapping(data.get("binding"), "binding"),
            usage=data.get("usage"),
            raw=_optional_str(data.get("raw"), "raw"),
            cog=_optional_mapping(data.get("cog"), "cog"),
            task=_optional_str(data.get("task"), "task"),
            timing=_optional_mapping(data.get("timing"), "timing"),
        )

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape of ``result-envelope.md``, as a worker would send it."""
        return {
            "envelope": self.envelope,
            "cog": dict(self.cog) if self.cog is not None else None,
            "task": self.task,
            "ok": self.ok,
            "error": {"code": self.error.code, "detail": self.error.detail} if self.error is not None else None,
            "payload": self.payload,
            "raw": self.raw,
            "problems": [{"check": p.check, "detail": p.detail, "severity": p.severity} for p in self.problems],
            "binding": dict(self.binding) if self.binding is not None else None,
            "usage": self.usage,
            "timing": dict(self.timing) if self.timing is not None else None,
        }


def _parse_error(value: Any) -> EnvelopeError | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise EnvelopeInvalid("error must be null or {code, detail}")
    code = value.get("code")
    if not isinstance(code, str) or not code:
        raise EnvelopeInvalid("error.code must be a non-empty string")
    detail = value.get("detail", "")
    if detail is None:
        detail = ""
    if not isinstance(detail, str):
        raise EnvelopeInvalid("error.detail must be a string")
    return EnvelopeError(code, detail)


def _parse_problems(value: Any) -> tuple[Problem, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise EnvelopeInvalid("problems must be a list")
    problems = []
    for item in value:
        if not isinstance(item, Mapping):
            raise EnvelopeInvalid("each problem is {check, detail, severity}")
        check, detail, severity = item.get("check"), item.get("detail", ""), item.get("severity", "error")
        if not isinstance(check, str) or not check:
            raise EnvelopeInvalid("problem.check must be a non-empty string")
        if not isinstance(detail, str):
            raise EnvelopeInvalid("problem.detail must be a string")
        if severity not in SEVERITIES:
            raise EnvelopeInvalid(f"problem.severity must be one of {SEVERITIES}")
        problems.append(Problem(check, detail, severity))
    return tuple(problems)


def _optional_mapping(value: Any, name: str) -> Mapping[str, Any] | None:
    if value is not None and not isinstance(value, Mapping):
        raise EnvelopeInvalid(f"{name} must be null or an object")
    return value


def _optional_str(value: Any, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise EnvelopeInvalid(f"{name} must be null or a string")
    return value
