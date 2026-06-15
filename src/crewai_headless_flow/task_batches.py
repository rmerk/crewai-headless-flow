"""Helpers for conservative task batching in do_work."""

from __future__ import annotations

from .state import TaskItem


def ready_execution_tasks(
    tasks: list[TaskItem],
    *,
    allowed_task_ids: set[int] | None = None,
) -> list[TaskItem]:
    """Return ready tasks eligible for next structured do_work attempt."""

    completed = {task.id for task in tasks if task.status == "done"}
    return [
        task
        for task in sorted(tasks, key=lambda item: item.id)
        if allowed_task_ids is None or task.id in allowed_task_ids
        if task.status in {"pending", "needs_revision", "failed"}
        and all(dependency in completed for dependency in task.dependencies)
    ]


def select_execution_batch(
    tasks: list[TaskItem],
    max_workers: int,
    *,
    allowed_task_ids: set[int] | None = None,
) -> list[TaskItem]:
    """Select next safe batch of ready tasks."""

    ready = ready_execution_tasks(tasks, allowed_task_ids=allowed_task_ids)
    if not ready:
        return []

    batch = _largest_disjoint_known_file_batch(ready, limit=max(1, max_workers))
    return batch if len(batch) > 1 else [ready[0]]


def has_pending_tasks(
    tasks: list[TaskItem],
    *,
    allowed_task_ids: set[int] | None = None,
) -> bool:
    return any(
        task.status != "done"
        and (allowed_task_ids is None or task.id in allowed_task_ids)
        for task in tasks
    )


def _normalized_files(task: TaskItem) -> set[str]:
    return {path.strip() for path in task.files if path.strip()}


def _largest_disjoint_known_file_batch(
    ready: list[TaskItem], *, limit: int
) -> list[TaskItem]:
    candidates = [task for task in ready if _normalized_files(task)]
    if not candidates or limit <= 1:
        return []

    best_ids: tuple[int, ...] = ()
    best_batch: list[TaskItem] = []

    def backtrack(
        index: int,
        current_batch: list[TaskItem],
        current_ids: tuple[int, ...],
        seen_files: set[str],
    ) -> None:
        nonlocal best_ids, best_batch

        if len(current_batch) > len(best_batch) or (
            len(current_batch) == len(best_batch)
            and current_batch
            and (not best_ids or current_ids < best_ids)
        ):
            best_ids = current_ids
            best_batch = list(current_batch)

        if len(current_batch) >= limit or index >= len(candidates):
            return
        if len(current_batch) + (len(candidates) - index) <= len(best_batch):
            return

        for next_index in range(index, len(candidates)):
            task = candidates[next_index]
            task_files = _normalized_files(task)
            if task_files & seen_files:
                continue
            current_batch.append(task)
            backtrack(
                next_index + 1,
                current_batch,
                (*current_ids, task.id),
                seen_files | task_files,
            )
            current_batch.pop()

    backtrack(0, [], (), set())
    return best_batch
