"""The distilled model's input format, shared by training (distill/build.py) and serving
(the llamacpp provider), so the two can never drift apart.

A narrow task's long system prompt (e.g. Banking77's list of 77 labels) is replaced by a short
task tag: the fine-tuned model has learned the task, so it doesn't need the instructions. That
makes training examples short enough for CPU training, and inference cheaper.
"""

from gateway.core.schemas import ChatMessage


def task_tag(task: str) -> str:
    return f"task: {task}"


def last_user_text(messages: list[ChatMessage]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            content = m.content
            if isinstance(content, list):
                return " ".join(str(p.get("text", "")) for p in content if p.get("type") == "text")
            return content or ""
    return ""


def distilled_messages(task: str, user_text: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": task_tag(task)}, {"role": "user", "content": user_text}]
