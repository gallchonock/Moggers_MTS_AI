"""Build a local knowledge index and produce submission.csv."""

import csv
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
from queue import Empty, Queue
import sys
from tempfile import NamedTemporaryFile
from threading import Thread
from time import monotonic
from urllib.parse import urlsplit
from zipfile import BadZipFile

from consultant import EMPTY_QUESTION_ANSWER, SERVICE_FALLBACK, extract_query, generate_answer, validate_answer
from contracts import Evidence, Question
from llm_client import Proxy, ProxyError
from retrieval import build_index, chunk_documents, load_kb, retrieve


@dataclass(frozen=True)
class Config:
    data_dir: Path
    url: str
    key: str = field(repr=False)
    model: str
    temperature: float
    max_tokens: int
    workers: int
    top_k: int
    top_n: int
    reranker_model: str
    deadline_seconds: float


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _number(name, default, lower, upper=None):
    try:
        value = type(default)(_env(name, default=str(default)))
        finite = isinstance(value, int) or math.isfinite(value)
    except ValueError:
        return default
    if not finite or value < lower or (upper is not None and value > upper):
        return default
    return value


def load_config() -> Config:
    return Config(
        data_dir=Path(_env("DATA_DIR", default="data")),
        url=_env("LLM_BASE_URL", "OPENAI_BASE_URL"),
        key=_env("LLM_API_KEY", "OPENAI_API_KEY"),
        model=_env("LLM_MODEL", "OPENAI_MODEL", "GEN_MODEL"),
        temperature=_number("TEMPERATURE", 0.0, 0.0, 2.0),
        max_tokens=_number("MAX_TOKENS", 2048, 1),
        workers=_number("MAX_WORKERS", 3, 1, 8),
        top_k=_number("TOP_K", 12, 1, 100),
        top_n=_number("RERANKER_TOP_N", 5, 1, 30),
        reranker_model=(
            _env("RERANKER_MODEL") if _env("MVP_ENABLE_RERANK") == "1" else ""
        ),
        deadline_seconds=_number("MVP_DEADLINE_SECONDS", 1080.0, 0.1, 1080.0),
    )


def find_inputs(data_dir: Path) -> tuple[Path, Path]:
    def first(names: tuple[str, ...]) -> Path:
        for name in names:
            path = data_dir / name
            if path.is_file():
                return path
        raise FileNotFoundError(names[0])

    return first(("questions.csv", "train.csv")), first(("knowledge-base.zip", "knowledge_base.zip", "kb.zip"))


def read_questions(path: Path) -> list[Question]:
    questions = []
    seen = set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.reader(stream, strict=True)
        header = next(rows, [])
        if len(header) != 2 or set(header) != {"id", "question"}:
            raise ValueError("invalid_questions_header")
        id_column = header.index("id")
        for row in rows:
            if len(row) != 2:
                raise ValueError(f"invalid_question_record_{rows.line_num}")
            question_id = row[id_column]
            if not question_id.strip() or question_id in seen:
                raise ValueError(f"invalid_question_id_at_line_{rows.line_num}")
            seen.add(question_id)
            questions.append(Question(question_id, row[1 - id_column]))
    return questions


def write_submission(path: Path, questions: list[Question], answers: list[str]) -> None:
    if len(questions) != len(answers):
        raise ValueError("answer_count_mismatch")
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", delete=False, dir=path.parent,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(["id", "answer"])
            writer.writerows((question.id, answer) for question, answer in zip(questions, answers))
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _answer(proxy: Proxy, index, question: Question, config: Config):
    query = extract_query(question.text)
    candidates = retrieve(index, query, config.top_k)
    selected = candidates[:config.top_n]
    rerank_error = None
    if config.reranker_model and len(candidates) > 1 and not proxy.rerank_auth_failed.is_set():
        documents = [
            f"{item.chunk.path}\n{item.chunk.heading}\n{item.chunk.text}" for item in candidates
        ]
        try:
            order = proxy.rerank(
                config.reranker_model, query.search_text, documents, min(config.top_n, len(candidates)),
            )
            selected = [Evidence(candidates[index].chunk, rank) for rank, index in enumerate(order, 1)]
        except ProxyError as exc:
            rerank_error = str(exc)
    result = generate_answer(proxy, query, selected, config)
    return validate_answer(result), rerank_error


def _process(proxy: Proxy, index, questions: list[Question], config: Config, deadline: float):
    pending = iter(enumerate(questions))
    output = Queue()
    tasks = Queue()
    answers = [SERVICE_FALLBACK if question.text.strip() else EMPTY_QUESTION_ANSWER for question in questions]
    completed = 0
    failures = 0

    def worker():
        while monotonic() < deadline:
            job = tasks.get()
            if job is None:
                return
            position, question = job
            try:
                answer, rerank_error = _answer(proxy, index, question, config)
                output.put((position, answer, None, rerank_error))
            except ProxyError as exc:
                output.put((position, SERVICE_FALLBACK, str(exc), None))
            except Exception as exc:
                output.put((position, SERVICE_FALLBACK, type(exc).__name__, None))

    def schedule() -> bool:
        job = next(pending, None)
        if job is None or monotonic() >= deadline or proxy.auth_failed.is_set():
            return False
        tasks.put(job)
        return True

    workers = []
    active = 0
    for _ in range(min(len(questions), config.workers)):
        thread = Thread(target=worker, daemon=True)
        thread.start()
        workers.append(thread)
        active += schedule()

    while active:
        remaining = max(0.0, deadline - monotonic())
        try:
            position, answer, error, rerank_error = output.get(timeout=remaining)
        except Empty:
            break
        active -= 1
        completed += 1
        answers[position] = answer
        if error:
            failures += 1
            print(f"row={position + 1} stage=answer error={error}", file=sys.stderr)
        if rerank_error:
            print(f"row={position + 1} stage=rerank error={rerank_error}", file=sys.stderr)
        if proxy.throttled < 2 or active == 0:
            active += schedule()

    for _ in workers:
        tasks.put(None)
    # A blocked transport must not keep the process alive beyond the CSV deadline.
    if not active:
        for thread in workers:
            thread.join()
        proxy.close()
    return answers, completed, failures


def main() -> int:
    started = monotonic()
    config = load_config()
    deadline = started + config.deadline_seconds
    try:
        questions_path, kb_path = find_inputs(config.data_dir)
        try:
            questions = read_questions(questions_path)
        except (OSError, ValueError, csv.Error) as exc:
            detail = type(exc).__name__
            if isinstance(exc, csv.Error):
                detail = "invalid_csv"
            elif isinstance(exc, ValueError) and str(exc).startswith("invalid_question"):
                detail = str(exc)
            print(f"stage=input file=questions.csv error={detail}", file=sys.stderr)
            return 2
        try:
            documents = load_kb(kb_path)
        except (OSError, ValueError, BadZipFile, RuntimeError) as exc:
            print(f"stage=input file=knowledge-base.zip error={type(exc).__name__}", file=sys.stderr)
            return 2
        index = build_index(chunk_documents(documents))
        parsed = urlsplit(config.url)
        if not (parsed.scheme in ("http", "https") and parsed.hostname and config.key and config.model):
            print("stage=config error=missing_or_invalid_proxy_settings", file=sys.stderr)
            return 2
        proxy = Proxy(
            config.url, config.key, config.model, config.temperature, config.max_tokens,
            deadline, config.workers,
        )
        answers, completed, failures = _process(proxy, index, questions, config, deadline)
        write_submission(Path.cwd() / "submission.csv", questions, answers)
    except FileNotFoundError as exc:
        missing = exc.args[0] if exc.args else None
        if missing in ("questions.csv", "knowledge-base.zip"):
            print(f"stage=input error=missing_file file={missing}", file=sys.stderr)
        else:
            print("stage=run error=file_not_found", file=sys.stderr)
        return 2
    except (OSError, ValueError, csv.Error, BadZipFile) as exc:
        print(f"stage=run error={type(exc).__name__}", file=sys.stderr)
        return 2
    pending = len(questions) - completed
    print(
        f"questions={len(questions)} completed={completed} fallbacks={failures + pending} "
        f"unprocessed={pending} deadline={int(monotonic() >= deadline)} "
        f"requests={proxy.requests} retries={proxy.retries} rate_limits={proxy.throttled} "
        f"seconds={monotonic() - started:.2f}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
