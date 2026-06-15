from __future__ import annotations

import pytest

from crewai_headless_flow.state import TaskItem
from crewai_headless_flow.task_batches import (
    has_pending_tasks,
    ready_execution_tasks,
    select_execution_batch,
)


pytestmark = pytest.mark.offline


def test_select_execution_batch_returns_independent_ready_tasks():
    tasks = [
        TaskItem(id=1, description="one", files=["src/a.py"]),
        TaskItem(id=2, description="two", files=["src/b.py"]),
        TaskItem(id=3, description="three", files=["src/c.py"], dependencies=[1]),
    ]

    batch = select_execution_batch(tasks, max_workers=2)

    assert [task.id for task in batch] == [1, 2]


def test_select_execution_batch_avoids_overlapping_files():
    tasks = [
        TaskItem(id=1, description="one", files=["src/shared.py"]),
        TaskItem(id=2, description="two", files=["src/shared.py"]),
    ]

    batch = select_execution_batch(tasks, max_workers=2)

    assert [task.id for task in batch] == [1]


def test_select_execution_batch_prefers_largest_safe_disjoint_batch():
    tasks = [
        TaskItem(id=2, description="broad", files=["src/a.py", "src/b.py"]),
        TaskItem(id=3, description="a", files=["src/a.py"]),
        TaskItem(id=4, description="b", files=["src/b.py"]),
        TaskItem(id=5, description="c", files=["src/c.py"]),
    ]

    batch = select_execution_batch(tasks, max_workers=3)

    assert [task.id for task in batch] == [3, 4, 5]


def test_select_execution_batch_requires_completed_dependencies():
    tasks = [
        TaskItem(id=1, description="one", files=["src/a.py"], status="done"),
        TaskItem(id=2, description="two", files=["src/b.py"], dependencies=[1]),
        TaskItem(id=3, description="three", files=["src/c.py"], dependencies=[4]),
    ]

    batch = select_execution_batch(tasks, max_workers=3)

    assert [task.id for task in batch] == [2]


def test_select_execution_batch_includes_needs_revision_tasks():
    tasks = [
        TaskItem(
            id=1,
            description="one",
            files=["src/a.py"],
            status="needs_revision",
        ),
        TaskItem(id=2, description="two", files=["src/b.py"], status="done"),
    ]

    batch = select_execution_batch(tasks, max_workers=2)

    assert [task.id for task in batch] == [1]


def test_ready_execution_tasks_returns_sorted_ready_frontier():
    tasks = [
        TaskItem(id=3, description="three", files=["src/c.py"], dependencies=[1]),
        TaskItem(id=1, description="one", files=["src/a.py"], status="done"),
        TaskItem(id=2, description="two", files=["src/b.py"], status="failed"),
        TaskItem(id=4, description="four", files=["src/d.py"], dependencies=[9]),
    ]

    ready = ready_execution_tasks(tasks)

    assert [task.id for task in ready] == [2, 3]


def test_ready_execution_tasks_honors_allowed_task_ids():
    tasks = [
        TaskItem(id=1, description="one", files=["src/a.py"]),
        TaskItem(id=2, description="two", files=["src/b.py"]),
        TaskItem(id=3, description="three", files=["src/c.py"], dependencies=[1]),
    ]

    ready = ready_execution_tasks(tasks, allowed_task_ids={2})

    assert [task.id for task in ready] == [2]


def test_select_execution_batch_honors_allowed_task_ids():
    tasks = [
        TaskItem(id=1, description="one", files=["src/a.py"]),
        TaskItem(id=2, description="two", files=["src/b.py"]),
        TaskItem(id=3, description="three", files=["src/c.py"]),
    ]

    batch = select_execution_batch(tasks, max_workers=3, allowed_task_ids={2, 3})

    assert [task.id for task in batch] == [2, 3]


def test_has_pending_tasks_honors_allowed_task_ids():
    tasks = [
        TaskItem(id=1, description="one", files=["src/a.py"], status="done"),
        TaskItem(id=2, description="two", files=["src/b.py"], status="pending"),
    ]

    assert has_pending_tasks(tasks, allowed_task_ids={1}) is False
    assert has_pending_tasks(tasks, allowed_task_ids={2}) is True
