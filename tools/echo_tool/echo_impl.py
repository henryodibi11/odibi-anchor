"""Echo tool implementation — proof-of-concept for Anchor Tool Registry.

Entry point: echo_context(message, repeat=1, output_format="dict")
Returns a standard Anchor contract echoing back the provided message.
"""
from __future__ import annotations


def echo_context(
    *args,
    message: str = "",
    repeat: int = 1,
    output_format: str = "dict",
    **kwargs,
) -> dict | str:
    """Echo a message back as a standard Anchor contract.

    Args:
        message: The message to echo (required by tool.json spec).
        repeat: Number of times to repeat the message (default 1).
        output_format: "dict" or "markdown".

    Returns:
        Standard Anchor contract dict (or markdown string).
    """
    # Accept message as first positional arg too
    if args and not message:
        message = str(args[0])

    if not message:
        raise ValueError("echo tool requires 'message' argument")

    repeated = message if repeat <= 1 else "\n".join([message] * repeat)

    result = {
        "kind": "echo",
        "subject": "echo",
        "summary": f"Echo: {message[:50]}{'...' if len(message) > 50 else ''}",
        "metrics": {
            "message_length": len(message),
            "repeat": repeat,
            "output_length": len(repeated),
        },
        "findings": [repeated],
        "risks": [],
        "samples": {"raw_message": message},
        "suggested_next_actions": [
            "This is a proof-of-concept tool registered via tool.json.",
            "Run anchor(\'tools\') to see all registered tools.",
        ],
    }

    if output_format == "markdown":
        lines = [
            f"# Echo\n",
            f"**{result['summary']}**\n",
            f"## Message\n",
            f"```\n{repeated}\n```\n",
            f"## Metrics\n",
            f"| Metric | Value |",
            f"| --- | --- |",
        ]
        for k, v in result["metrics"].items():
            lines.append(f"| {k} | {v} |")
        return "\n".join(lines)

    return result
