# Place scrape queue: design

## Intent

`PlaceScraper.scrape()` runs one scrape per keyword, and each keyword may be
split into a grid of cells. Today the keywords run in a plain loop, so a crash
part-way through a keyword loses that keyword's progress and the run cannot
pick up where it stopped.

This change puts the keywords of a city run into a Redis queue. A run that stops
part-way resumes from the cell it stopped at when the same command is run again.

## Goals

- `PlaceScraper` creates a Redis-backed queue for each city run.
- `_scrape_keyword` no longer runs the keyword. It stores the keyword's job,
  with all the data it needs, in the queue.
- A listener takes the jobs in FIFO order, one at a time. Each job runs on its
  own thread.
- A job saves its progress (the next grid cell and the rows so far) after every
  cell, so it can resume from that cell.
- Re-running the same city command resumes its unfinished queue automatically.
- The queue is deleted when the run ends.

## Non-goals

- Queues shared between different city runs. Each run has its own queue.
- Parallel keyword jobs. Google is searched one scrape at a time.
- Changes to `ScrapeSession`, `run_scrape`, the row and state buffers, or the
  CLI.

## Anti-goals

- A crash must never lose a finished keyword's leads or make a finished keyword
  run again.
- The caller of `scrape()` sees the same `ScrapeResult` and the same exceptions
  as today.

## Behaviour

**Interface.** `PlaceScraper.scrape(query, known_keys)` still blocks until every
keyword has finished, and returns the same `ScrapeResult`. `salvage_rows` still
works during a Ctrl-C.

**Run identity.** The run's queue is named after a hash of the full `PlaceQuery`
(the settings the user typed). Running the same command again finds the same
queue. A different query starts a new queue. *(My call: the approved design
matched by city, state and keyword list. I used the full query, so a re-run
with a different grid or filter does not resume a queue built for the old
settings.)*

**Enqueue.** On a fresh run, `scrape()` geocodes the city, then calls
`_scrape_keyword` once per keyword. Each call pushes one job to the end of the
queue. A geocoding failure returns early, as today, and leaves no queue.

**Job.** A job holds: the keyword, the scrape name, the keyword's settings, the
bounding box (or none), the grid cells (or none for a single scrape), the index
of the next cell, the deduped rows and the kept rows so far, and the finished
and skipped counts.

**Listener.** The listener runs in the thread that called `scrape()`. It takes
the first job in the queue and runs it on a new thread, then waits for that
thread. An exception raised in the job thread is re-raised in the listener.
Jobs run in FIFO order, one at a time.

**Progress.** Before each grid cell, the job's current state is written back to
the head of the queue. A resume therefore runs the cell that was in progress
again, from its saved state. The dedupe check uses the saved rows, so cells
already finished are not searched again.

**Completion.** When a job finishes, its result is appended to the done list and
the job is removed from the queue, in one Redis transaction. A job that stops the
run (rate limit, or lost Chrome connection) ends the run: the remaining jobs are
dropped, as the current loop does with `break`.

**Clearing.** When the run ends normally, or is stopped by a rate limit or a lost
Chrome connection, the queue and the done list are deleted. If the process
crashes or raises an error, the queue is kept, so the next run resumes it.

**Known keys.** A job's known keys are the caller's `known_keys` plus the keys
of the leads kept by the finished jobs, as today.

**Salvage.** `salvage_rows` holds the rows of the finished jobs plus the kept
rows of the job that is running.

## Constraints

- Redis is the queue store. Keys are namespaced `place_queue:<run id>:` and have
  the same TTL as the other buffers (`BUFFER_TTL_SECONDS`).
- Redis errors are raised as `BufferError`, as elsewhere.
- Jobs are stored as JSON, so the cell bounding boxes are lists when read back.

## Left to the builder

- Naming of the queue module and its methods, as long as it follows the code
  around it.
- Whether to add a thread-safe wrapper. The listener is the only writer, so one
  is not needed.
