"""Black-box checks for the submission entry point, using a local proxy stub."""

import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from consultant import build_messages, extract_query
from contracts import Document
from retrieval import build_index, chunk_documents, retrieve


ROOT = Path(__file__).resolve().parents[1]
SUBMISSION_FILES = (
    "solution.py",
    "requirements.txt",
    "contracts.py",
    "retrieval.py",
    "consultant.py",
    "llm_client.py",
)
KEY = "local-test-key-no-external-access"
MODEL = "test-model"
ANSWER = 'Подойдёт учёт, если прибор совместим. Уточните модель "А-1".'
SECRET_BODY = "private-response-body-marker"
ENV_NAMES = (
    "DATA_DIR", "LLM_BASE_URL", "OPENAI_BASE_URL", "LLM_API_KEY",
    "OPENAI_API_KEY", "LLM_MODEL", "OPENAI_MODEL", "GEN_MODEL", "TEMPERATURE",
    "MAX_TOKENS", "MAX_WORKERS", "TOP_K", "CHUNKER", "EMB_MODEL",
    "RERANKER_MODEL", "RERANKER_TOP_N", "MVP_ENABLE_RERANK",
    "MVP_DEADLINE_SECONDS", "PYTHONPATH",
)


def completion(content=ANSWER):
    return {"choices": [{"message": {"content": content}}]}


def prompt_contains(payload, marker):
    return any(marker in message["content"] for message in payload["messages"])


@contextmanager
def proxy_stub(route, *, chunk_delay=0):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def handle(self):
            try:
                super().handle()
            except (ConnectionAbortedError, ConnectionResetError):
                pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), payload))
            status, response = route(payload)
            body = response if isinstance(response, bytes) else json.dumps(response).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Retry-After", "0")
                self.end_headers()
                if chunk_delay:
                    for byte in body:
                        self.wfile.write(bytes((byte,)))
                        time.sleep(chunk_delay)
                else:
                    self.wfile.write(body)
            except OSError:
                # A canceled client request may close the connection early.
                pass

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1/", requests
        finally:
            server.shutdown()
            thread.join()


class MvpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="energytool-tests-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.run_dir = self.directory / "run"
        self.run_dir.mkdir()
        self.data_dir = self.directory / "input"
        self.data_dir.mkdir()
        self.entrypoint = ROOT / "solution.py"

        with ZipFile(self.data_dir / "knowledge-base.zip", "w", ZIP_DEFLATED) as kb:
            kb.writestr(
                "products/meter.md",
                b"\xef\xbb\xbf" + (
                    "# Учёт электроэнергии\n\n"
                    "Учёт электроэнергии подключают после проверки совместимости прибора. "
                    "В тестовом комплексе модель прибора А-1 поддерживается."
                ).encode("utf-8"),
            )
            kb.writestr("notes.txt", "Уточните протокол прибора перед подключением.")
            kb.writestr("__MACOSX/skip.md", "Ненужная служебная запись.")

    def write_questions(self, rows):
        with (self.data_dir / "questions.csv").open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["id", "question"])
            writer.writerows(rows)

    def run_solution(self, url, extra_env=None):
        env = {name: value for name, value in os.environ.items() if name not in ENV_NAMES}
        env.update({
            "DATA_DIR": str(self.data_dir),
            "OPENAI_BASE_URL": url,
            "OPENAI_API_KEY": KEY,
            "GEN_MODEL": MODEL,
            "PYTHONIOENCODING": "utf-8",
        })
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(self.entrypoint)],
            cwd=self.run_dir,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
        )

    def read_answers(self, result, expected_ids):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        self.assertNotIn(SECRET_BODY, result.stdout + result.stderr)
        with (self.run_dir / "submission.csv").open(encoding="utf-8", newline="") as file:
            rows = list(csv.reader(file, strict=True))
        self.assertEqual(rows[0], ["id", "answer"])
        self.assertTrue(all(len(row) == 2 for row in rows))
        self.assertEqual([row[0] for row in rows[1:]], expected_ids)
        answers = [row[1] for row in rows[1:]]
        self.assertTrue(all(answer.strip() and len(answer) <= 10000 for answer in answers))
        return answers

    def test_topic_change_searches_current_task_and_preserves_last_turn(self):
        docs = [
            Document("solar.md", "# Солнечные электростанции\n\nСолнечная станция для магазина."),
            Document(
                "meter.md",
                "# Облачная платформа учёта\n\n"
                "## Кому подходит\n\nРетейл: сравнение потребления по магазинам.",
            ),
        ]
        latest = "Станцию отложим. Как сравнить потребление восьми магазинов?"
        query = extract_query(f"Пользователь: Обсудим солнечную станцию.\nПользователь: {latest}")
        candidates = retrieve(build_index(chunk_documents(docs)), query)
        payload = json.loads(build_messages(query, candidates)[1]["content"])
        self.assertNotIn("солнечную станцию", query.search_text)
        self.assertEqual(payload["последняя_реплика"], latest)
        self.assertEqual(payload["фрагменты_базы"][0]["file"], "meter.md")

    def test_unpacked_submission_csv_and_proxy_aliases(self):
        archive = self.directory / "solution.zip"
        unpacked = self.directory / "unpacked"
        with ZipFile(archive, "w", ZIP_DEFLATED) as output:
            for name in SUBMISSION_FILES:
                output.write(ROOT / name, name)
        with ZipFile(archive) as zipped:
            self.assertEqual(set(zipped.namelist()), set(SUBMISSION_FILES))
            self.assertEqual(zipped.read("requirements.txt").strip(), b"httpx==0.28.1")
            zipped.extractall(unpacked)
        self.entrypoint = unpacked / "solution.py"
        self.write_questions([
            ("0007", 'Учёт электроэнергии, медленный сценарий.\nПодходит ли прибор "А-1"?'),
            ("8", "Учёт электроэнергии, быстрый сценарий."),
            ("9", ""),
        ])
        first_started = threading.Event()
        second_ready = threading.Event()

        def route(payload):
            if prompt_contains(payload, "медленный сценарий"):
                first_started.set()
                second_ready.wait(3)
                time.sleep(0.1)
                return 200, completion(ANSWER + "\nТребуется проверить схему.")
            first_started.wait(3)
            second_ready.set()
            return 200, completion("Проверьте протокол счётчика.")

        with proxy_stub(route) as (url, requests):
            result = self.run_solution(url, {"MAX_WORKERS": "2", "MAX_TOKENS": "512"})
        answers = self.read_answers(result, ["0007", "8", "9"])
        self.assertIn(ANSWER, answers[0])
        self.assertIn("Требуется проверить схему.", answers[0])
        self.assertEqual(answers[1], "Проверьте протокол счётчика.")
        self.assertEqual(len(requests), 2)
        for path, authorization, payload in requests:
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(authorization, f"Bearer {KEY}")
            self.assertEqual(payload["model"], MODEL)
            self.assertEqual(payload["max_tokens"], 512)
            self.assertEqual(payload["temperature"], 0)
            self.assertTrue(prompt_contains(payload, "products/meter.md"))
        self.assertFalse((unpacked / "submission.csv").exists())

    def test_retry_limits_and_invalid_model_responses(self):
        markers = ("маркер429", "маркер503", "маркербитый", "маркерпустой", "маркертекст")
        self.write_questions([(str(i), f"Учёт электроэнергии, {marker}") for i, marker in enumerate(markers)])
        counts = dict.fromkeys(markers, 0)

        def route(payload):
            marker = next(marker for marker in markers if prompt_contains(payload, marker))
            counts[marker] += 1
            if marker == "маркер429" and counts[marker] == 1:
                return 429, {"error": SECRET_BODY}
            if marker == "маркер503":
                return 503, {"error": SECRET_BODY}
            if marker == "маркербитый":
                return 200, SECRET_BODY.encode()
            if marker == "маркерпустой":
                return 200, completion(" \n ")
            if marker == "маркертекст":
                return 200, completion((ANSWER + " ") * 350)
            return 200, completion()

        with proxy_stub(route) as (url, _):
            result = self.run_solution(url, {"MAX_WORKERS": "1"})
        answers = self.read_answers(result, ["0", "1", "2", "3", "4"])
        self.assertEqual(counts, dict(zip(markers, (2, 2, 1, 1, 1))))
        self.assertEqual(answers[0], ANSWER)
        self.assertIn(ANSWER, answers[4])

    def test_unauthorized_stops_requests_and_keeps_output(self):
        self.write_questions([
            ("1", "Учёт электроэнергии."),
            ("2", "Учёт электроэнергии для второго прибора."),
        ])
        with proxy_stub(lambda _: (401, {"error": SECRET_BODY})) as (url, requests):
            result = self.run_solution(url, {"MAX_WORKERS": "1"})
        self.read_answers(result, ["1", "2"])
        self.assertEqual(len(requests), 1)
        self.assertIn("401", result.stdout + result.stderr)

    def test_partial_rerank_keeps_local_evidence(self):
        self.write_questions([("1", "Учёт электроэнергии, уточните протокол прибора перед подключением.")])

        def route(payload):
            if "documents" in payload:
                return 200, {"results": [{"index": 1}]}
            return 200, completion()

        with proxy_stub(route) as (url, requests):
            result = self.run_solution(url, {
                "MVP_ENABLE_RERANK": "1", "RERANKER_MODEL": "test-reranker", "RERANKER_TOP_N": "2",
            })
        self.assertEqual(self.read_answers(result, ["1"]), [ANSWER])
        self.assertEqual([path for path, _, _ in requests], ["/v1/rerank", "/v1/chat/completions"])
        rerank_payload = requests[0][2]
        chat_payload = requests[1][2]
        self.assertEqual(rerank_payload["model"], "test-reranker")
        self.assertEqual(len(rerank_payload["documents"]), 2)
        self.assertTrue(prompt_contains(chat_payload, "products/meter.md"))
        self.assertTrue(prompt_contains(chat_payload, "notes.txt"))

    def test_deadline_writes_all_ids_while_request_is_still_running(self):
        self.write_questions([
            ("1", "Учёт электроэнергии, долгий сценарий."),
            ("2", "Учёт электроэнергии."),
        ])

        with proxy_stub(lambda _: (200, completion()), chunk_delay=0.1) as (url, requests):
            started = time.monotonic()
            result = self.run_solution(url, {"MAX_WORKERS": "1", "MVP_DEADLINE_SECONDS": "1.5"})
            elapsed = time.monotonic() - started
        self.read_answers(result, ["1", "2"])
        self.assertEqual(len(requests), 1)
        self.assertLess(elapsed, 5, "The process waited for a stalled HTTP request past the deadline.")

    def test_invalid_and_missing_inputs_fail_before_request(self):
        with proxy_stub(lambda _: (200, completion())) as (url, requests):
            result = self.run_solution(url)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("questions.csv", result.stdout + result.stderr)
            self.write_questions([("1", "Учёт электроэнергии."), ("1", "Подходит ли прибор?")])
            result = self.run_solution(url)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((result.stdout + result.stderr).strip())
            self.write_questions([("1", "Учёт электроэнергии.")])
            kb_path = self.data_dir / "knowledge-base.zip"
            kb_path.unlink()
            result = self.run_solution(url)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("knowledge-base.zip", result.stdout + result.stderr)
            kb_path.write_bytes(b"not a valid ZIP")
            result = self.run_solution(url)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("BadZipFile", result.stdout + result.stderr)
        self.assertFalse(requests)
        self.assertFalse((self.run_dir / "submission.csv").exists())


if __name__ == "__main__":
    unittest.main()
