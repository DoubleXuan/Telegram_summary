import pytest

from tg_digest.scheduler import create_scheduler


async def callback() -> None:
    return None


async def failing_callback() -> None:
    raise RuntimeError("scheduled failure")


def test_scheduler_registers_digest_and_maintenance_jobs() -> None:
    scheduler = create_scheduler(
        "0 8-23/3 * * *",
        "Asia/Shanghai",
        callback,
        maintenance_cron="30 3 * * 0",
        maintenance_callback=callback,
    )

    assert {job.id for job in scheduler.get_jobs()} == {
        "telegram-digest",
        "telegram-digest-maintenance",
    }


@pytest.mark.asyncio
async def test_scheduler_propagates_callback_failure() -> None:
    scheduler = create_scheduler(
        "0 8-23/3 * * *",
        "Asia/Shanghai",
        failing_callback,
    )
    job = scheduler.get_job("telegram-digest")
    assert job is not None

    with pytest.raises(RuntimeError, match="scheduled failure"):
        await job.func()


def test_scheduler_requires_complete_maintenance_configuration() -> None:
    with pytest.raises(ValueError, match="configured together"):
        create_scheduler(
            "0 8-23/3 * * *",
            "Asia/Shanghai",
            callback,
            maintenance_cron="30 3 * * 0",
        )
