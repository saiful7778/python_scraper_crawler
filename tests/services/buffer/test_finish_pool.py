import threading

from app.services.buffer.finish_pool import FinishPool


def test_defer_does_not_wait_for_the_work():
    started = threading.Event()
    release = threading.Event()

    def work(scrape_id):
        started.set()
        release.wait(2)

    pool = FinishPool(work, workers=1)
    pool.defer("s1")

    assert started.wait(2)
    done = threading.Event()
    watcher = threading.Thread(target=lambda: (pool.drain(), done.set()))
    watcher.start()
    assert not done.wait(0.2)  # drain waits for the blocked work
    release.set()
    assert done.wait(2)
    pool.close()


def test_the_work_runs_on_a_worker_thread():
    caller = threading.current_thread()
    seen = []

    def work(scrape_id):
        seen.append(threading.current_thread())

    pool = FinishPool(work, workers=1)
    pool.defer("s1")
    pool.drain()

    assert len(seen) == 1
    assert seen[0] is not caller
    pool.close()


def test_two_workers_run_two_items_at_the_same_time():
    barrier = threading.Barrier(2, timeout=2)

    def work(scrape_id):
        barrier.wait()

    pool = FinishPool(work, workers=2)
    pool.defer("s1")
    pool.defer("s2")
    pool.drain()

    assert pool.error() is None
    pool.close()


def test_a_failed_item_is_recorded_and_does_not_stop_the_next_one():
    seen = []

    def work(scrape_id):
        seen.append(scrape_id)
        if scrape_id == "bad":
            raise RuntimeError("boom")

    pool = FinishPool(work, workers=1)
    pool.defer("bad")
    pool.defer("good")
    pool.drain()

    assert seen == ["bad", "good"]
    assert isinstance(pool.error(), RuntimeError)
    pool.close()
