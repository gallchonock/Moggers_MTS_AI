"""Build and query an in-memory lexical index of the EnergyTool knowledge base."""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath
from zipfile import ZipFile

from contracts import Chunk, Document, Evidence, Query


LOGGER = logging.getLogger(__name__)
TARGET_SIZE = 1100
MIN_CHUNK_SIZE = 800
HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)\s*#*\s*$")
ESCAPED_HEADING = re.compile(r" *\\(#{2,6} [^\r\n]+)")
TABLE_BORDER = re.compile(r"^[ \t]*-{5,}[ \t]*$")
TABLE_COLUMNS = re.compile(r"^[ \t]*-{2,}(?:[ \t]+-{2,})+[ \t]*$")
PIPE_COLUMNS = re.compile(r"^[ \t]*\|?[ \t]*:?-+:?(?:[ \t]*\|[ \t]*:?-+:?)+[ \t]*\|?[ \t]*$")
WORD = re.compile(r"квт[·⋅*×∙.]ч|[a-zа-я]+|\d+(?:[.,]\d+)?")
VOWEL = re.compile(r"[аеиоуыэюя]")
RUSSIAN_ENDING = re.compile(
    r"(иями|ями|ами|иях|ах|ях|иями|иею|ою|ею|ью|ая|яя|ое|ее|ые|ие|"
    r"ого|его|ому|ему|ыми|ими|ых|их|ую|юю|ой|ей|ый|ий|ом|ем|ам|ям|"
    r"ов|ев|ие|ия|ию|иям|ием|ению|енья|ении|ения|енье|ению|ени|ений|"
    r"а|я|ы|и|е|о|у|ю|ь)$"
)
VERB_ENDING = re.compile(r"(оваться|еваться|ироваться|ивать|ывать|ать|ять|еть|ить|ться|лся|лась|лись)$")
STOP_WORDS = frozenset(
    "а без бы был была были было в вам вас ваш ваша ваши вашей ведь во вот все всего "
    "вы где да даже для до его ее если есть еще же за зачем здесь и из или им их к как "
    "какая какие какой ко когда кто ли либо мы мне мочь можно можете может мог могу "
    "на надо нам нас наш наше не него нее нет ни но нужно нибудь об обо он она они "
    "оно от очень по под пока потом почему при про пусть раз с сам свою себе со "
    "так такая таких такой там те тем теперь то того тоже только том тут ты у уж уже "
    "хотеть хотим хочу чем через что чтобы это эта эти этот я вообще еще "
    "данных данный какие стоит надо надо ли свои свой таких сначала примерно "
    "кажется объяснить разобраться будет стоять стоят стоял наша наши ваших ваших "
    "другой другого другие других первых последующих сможете смог можно разберет "
    "иметь вид видеть хватит начинать начать понадобиться понадобится "
    "вашем нашем нашей вашей вашем нашими старый старая старые старого старых "
    "другим другом отдельно обычный обычная".split()
)
ALIASES = {
    "сим": "sim",
    "симка": "sim",
    "симки": "sim",
    "симок": "sim",
    "счетчик": "прибор_учета",
    "счетчика": "прибор_учета",
    "счетчики": "прибор_учета",
    "счетчиков": "прибор_учета",
    "счетчиках": "прибор_учета",
    "крыша": "кровля",
    "крыше": "кровля",
    "крышу": "кровля",
    "крыши": "кровля",
    "фасад": "вертикальная_поверхность",
    "фасаде": "вертикальная_поверхность",
    "вертикальные": "вертикальная_поверхность",
    "двухтарифные": "многотарифный",
    "двухтарифный": "многотарифный",
    "многотарифные": "многотарифный",
    "четвертая": "4",
    "четвертой": "4",
    "четвертую": "4",
    "магазин": "ретейл",
    "магазинов": "ретейл",
    "магазины": "ретейл",
    "ритейл": "ретейл",
    "фото": "фотограф",
    "фотографии": "фотограф",
    "фотография": "фотограф",
    "выгоден": "выгодн",
    "невыгоден": "невыгодн",
    "стоить": "стоимост",
}
CONCEPT_PREFIXES = {
    "expand_capacity": ("расшир", "наращ", "поэтап"),
    "metering_platform": ("аскуэ",),
    "slate_roof": ("шифер",),
}
CONCEPT_PHRASES = {
    name: re.compile(pattern)
    for name, pattern in {
        "metering_platform": r"\b(?:платформ\w*|автоматическ\w*|облачн\w*)\s+(?:коммерческ\w*\s+)?учет|\bучет\w*\s+электроэнерги",
        "metering_audit": r"\b(?:аудит\w*|провер\w*)\b[^.!?\n]{0,55}\b(?:учет|систем\w*\s+учет)",
        "reactive_energy": r"\bреактивн\w*\s+энерги",
        "photo_reading": r"\b(?:фото\w*|сним\w*|камер\w*)\b[^.!?\n]{0,65}\b(?:показан|счетчик|прибор|распозна|разбер|прочита)|\b(?:показан\w*|распозна\w*)\b[^.!?\n]{0,65}\b(?:фото|снимок)",
        "wholesale_market": r"\bоптов\w*\s+рын",
        "solar_station": r"\bсолнечн\w*\s+(?:(?:электро)?станц|панел)|\bсетев\w*\s+станц",
        "commercial_losses": r"\bкоммерческ\w*\s+потер",
        "department_metering": r"\b(?:по|между)\s+цех",
        "tenant_metering": r"\b(?:по|между)\s+арендатор",
        "site_metering": r"\bсравн\w*\s+(?:потреблен\w*\s+)?(?:\w+\s+)?магазин|\b(?:несколько\s+магазин|сетев\w*\s+ретейл|ретейл\w*\s*,\s*общепит)",
        "bill_increase": r"\b(?:рост|раст|скач|вырос)\w*\s+(?:\w+\s+){0,2}счет|\bсчет\w*\s+(?:\w+\s+){0,2}(?:рост|раст|скач|вырос)",
    }.items()
}
FACETS = {
    name: re.compile(pattern)
    for name, pattern in {
        "solar_siting": r"\b(?:скат\w*|фасад\w*|вертикальн\w*\s+поверхност\w*|ориентаци\w*|шифер\w*)",
        "station_modes": r"\b(?:сетев\w*\s+станц|вечерн\w*\s+(?:нагрузк|потреблен)|гибрид\w*\s+станц|два\s+типа\s+станций)",
        "meter_connection": r"\b(?:sim|симк\w*|схем\w*\s+сбора|совместим\w*\s+прибор)|\bсовместимость и инфраструктура",
        "recognised_fields": r"\b(?:тарифн\w*\s+зон\w*|год\w*\s+(?:выпуск|производств)\w*|двухтарифн\w*|что распознается)\b",
        "estimate": r"\b(?:сколько\s+(?:это\s+)?сто\w*|для\s+(?:предварительн\w*\s+)?оценк\w*|стоимост\w*\s+зависит)\b",
    }.items()
}
CATEGORY = re.compile(r"\bкатегори\w*\s*№?\s*([1-6])\b|\b([1-6])(?:\s*[-‑–]\s*(?:я|й|ю))?\s+(?:ценов\w*\s+)?категори")
CATEGORY_ORDINAL = r"(перв|втор|треть|четверт|пят|шест)(?:ая|ой|ую|ей|ью|я|ю|е)"
CATEGORY_WORDS = re.compile(r"\b" + CATEGORY_ORDINAL + r"\s+(?:ценов\w*\s+)?категори")
CATEGORY_ROW = re.compile(r"(?m)^\s*(?:\*\*)?" + CATEGORY_ORDINAL + r"\s*:")
CATEGORY_NUMBERS = {"перв": 1, "втор": 2, "треть": 3, "четверт": 4, "пят": 5, "шест": 6}
EVIDENCE_RULES = [
    # Use headings to distinguish a direct answer from passing mentions.
    (r"скат|фасад|вертикальн", r"размещение"),
    (r"скат|фасад|вертикальн", r"солнечные электростанции.*(?:в нашем регионе|как считается эффект)"),
    (r"шифер|ветх\w*\s+кровл", r"не подходит|ограничен"),
    (r"сетев\w*\s+(?:солнечн\w*\s+)?станц|гибрид|вечер\w*\s+(?:нагруз|потреблен)", r"два типа станций"),
    (r"отключен|резерв", r"если пропадет электричество"),
    (r"вечер\w*\s+(?:нагруз|потреблен)|(?:потребл\w*|отключен\w*).*(?:вечер|ночь)", r"солнечные электростанции.*(?:лучший профиль|не подходит)"),
    (r"\bsim\b|симк|перепрош|протокол связи", r"совместимость и инфраструктура|схему сбора данных"),
    (r"магазин.*(?:расход|потреблен|кабинет)|(?:расход|потреблен|кабинет).*магазин", r"ретейл"),
    (r"магазин.*(?:расход|потреблен|кабинет)|(?:расход|потреблен|кабинет).*магазин", r"облачная платформа учета.*кому подходит"),
    (r"цех.*(?:расход|потреблен|учет)|(?:расход|потреблен|учет).*цех", r"облачная платформа учета.*кому подходит"),
    (r"(?:фото|снимок).*(?:прибор|показан|счетчик|разбер|прочита|начислен|арендатор)|(?:счетчик|показан|снимок).*(?:фото|распозна)", r"верификаци|ограничение по применению|принимать такие показания"),
    (r"(?:фото|распозна).*(?:газ|вод|тепл)|(?:газ|вод|тепл).*(?:фото|распозна)", r"поддерживаемые ресурсы|какие ресурсы поддерживаются"),
    (r"счет\w*.*(?:вырос|провер|рост|увелич)|(?:вырос|провер|рост|увелич).*счет", r"что именно растет|из чего состоит счет"),
    (r"оптов\w*\s+рын|вы\w+\s+на\s+опт\b|участв\w*\s+в\s+опте", r"порог доступа определяется"),
    (r"сахалин|изолирован|неценов\w*\s+зон", r"где оптового рынка нет"),
    (r"несколько площадок|две площадки|электрически|сложить мощност", r"если у предприятия несколько площадок"),
    (r"миллион\w*\s+квт|окупаем|экономическ\w*\s+целесообраз", r"экономическая целесообразность"),
    (r"хищен|общ\w*\s+итог\s+за\s+сутки|суточн.*потер|потер.*суточн|архив.*потер|потер.*архив", r"поиск коммерческих потерь.*что мы не делаем"),
    (r"(?:архив|разбивк|сутки|суточн).*(?:потер|хищен)|(?:потер|хищен).*(?:архив|разбивк|сутки|суточн)", r"поиск коммерческих потерь.*что нужно от клиента|для поиска коммерческих потерь"),
    (r"(?:завод|промышлен|цех).*(?:коммерческ\w*\s+потер|нецелев)", r"не подходит промышленным потребителям"),
    (r"\bфио\b.*хищен|хищен.*\bфио\b", r"поиск коммерческих потерь.*передавать персональные данные"),
    (r"личн\w*\s+кабинет.*персональн|персональн.*личн\w*\s+кабинет", r"требуются ли персональные данные"),
    (r"отклонени.*план|план.*отклонени", r"когда подходят пятая и шестая категории"),
    (r"перепрош|протокол связи", r"общие границы"),
]
EVIDENCE_RULES = [(re.compile(query, re.DOTALL), re.compile(section)) for query, section in EVIDENCE_RULES]
PHOTO_VERIFICATION = re.compile(r"верификаци|ограничение по применению|принимать такие показания")
METERING_ESTIMATE = re.compile(r"стоимост|сколько.*сто|цен\w*|понадобится для оценки", re.DOTALL)
EVALUATION_INPUTS = re.compile(
    r"\b(?:для оценки|какие сведени|что нужно от клиент|сведения об объект|входные данные)"
)
PHOTO_SUBMISSION = re.compile(r"\b(?:присыла\w*|отправля\w*)\b[^.!?\n]{0,55}\b(?:фото|снимок)")
CONTEXT_TERMS = frozenset(
    {"телефон", "оставл", "оставля", "оценк", "понадобитс", "понадоб", "гот", "готов", "примерно", "начать", "нач"}
)


@dataclass(frozen=True)
class SearchIndex:
    chunks: tuple[Chunk, ...]
    postings: dict[str, tuple[tuple[int, float], ...]]
    idf: dict[str, float]
    length_penalties: tuple[float, ...]


def load_kb(path: str) -> list[Document]:
    """Read usable text members directly, without extracting archive paths."""
    documents = []
    seen = set()
    replacements = 0
    with ZipFile(path) as archive:
        for member in sorted(archive.infolist(), key=lambda item: item.filename):
            name = PurePosixPath(member.filename.replace("\\", "/"))
            if member.is_dir() or name.suffix.lower() not in {".md", ".txt"}:
                continue
            if name.is_absolute() or any(part.startswith(".") or part.casefold() == "__macosx" for part in name.parts):
                continue
            if name in seen:
                continue
            raw = archive.read(member)
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("utf-8-sig", errors="replace")
                replacements += text.count("\ufffd")
            if text.strip():
                seen.add(name)
                documents.append(Document(name.as_posix(), text))
    if not documents:
        raise ValueError("knowledge base contains no nonempty .md or .txt files")
    if replacements:
        LOGGER.warning("Knowledge base UTF-8 replacements: %d", replacements)
    return documents


def _sections(text: str) -> list[tuple[str, list[str]]]:
    sections = []
    headings: list[tuple[int, str]] = []
    lines: list[str] = []
    for line in ESCAPED_HEADING.sub(r"\n\n\1", text).splitlines():
        heading = HEADING.match(line)
        if heading:
            if lines:
                sections.append((" / ".join(title for _, title in headings), lines))
                lines = []
            depth = len(heading[1])
            while headings and headings[-1][0] >= depth:
                headings.pop()
            headings.append((depth, heading[2]))
        else:
            lines.append(line)
    if lines:
        sections.append((" / ".join(title for _, title in headings), lines))
    return sections


def _table_at(lines: list[str], start: int) -> tuple[int, int, int] | None:
    """Return header end, data end and table end for common Markdown tables."""
    if start + 1 >= len(lines):
        return None
    if TABLE_BORDER.fullmatch(lines[start]):
        for divider in range(start + 2, min(start + 5, len(lines))):
            if TABLE_COLUMNS.fullmatch(lines[divider]):
                end = divider + 1
                while end < len(lines) and not TABLE_BORDER.fullmatch(lines[end]):
                    end += 1
                if end < len(lines):
                    return divider + 1, end, end + 1
    if TABLE_COLUMNS.fullmatch(lines[start + 1]):
        end = start + 2
        while end < len(lines) and lines[end].strip():
            end += 1
        return start + 2, end, end
    if "|" in lines[start] and PIPE_COLUMNS.fullmatch(lines[start + 1]):
        end = start + 2
        while end < len(lines) and "|" in lines[end] and lines[end].strip():
            end += 1
        return start + 2, end, end
    return None


def _split_table(lines: list[str], start: int, bounds: tuple[int, int, int]) -> list[str]:
    header_end, data_end, end = bounds
    full = "\n".join(lines[start:end])
    if len(full) <= TARGET_SIZE:
        return [full]
    header = "\n".join(lines[start:header_end]) + "\n"
    footer = "\n" + lines[data_end] if data_end < end else ""
    data = "\n".join(lines[header_end:data_end]).strip("\n")
    if footer:
        # A blank line separates Pandoc multiline rows and their qualifications.
        rows = re.split(r"\n[ \t]*\n", data)
        separator = "\n\n"
    else:
        rows = data.splitlines()
        separator = "\n"
    result = []
    current = ""
    for row in rows:
        addition = separator + row if current else row
        if current and len(header) + len(current) + len(addition) + len(footer) > TARGET_SIZE:
            result.append(header + current + footer)
            current = row
        else:
            current += addition
    if current:
        result.append(header + current + footer)
    return result or [full]


def _blocks(lines: list[str]) -> list[str]:
    blocks = []
    paragraph = []
    pos = 0
    while pos < len(lines):
        table = _table_at(lines, pos)
        if table or not lines[pos].strip():
            if paragraph:
                blocks.append("\n".join(paragraph))
                paragraph = []
        if table:
            blocks.extend(_split_table(lines, pos, table))
            pos = table[2]
            continue
        if lines[pos].strip():
            paragraph.append(lines[pos])
        pos += 1
    if paragraph:
        blocks.append("\n".join(paragraph))
    return blocks


def _split_paragraph(text: str) -> list[str]:
    """Split oversized prose at sentences or whitespace."""
    pieces = []
    remaining = text
    while len(remaining) > TARGET_SIZE:
        boundaries = [match.end() for match in re.finditer(r"[.!?](?=\s)", remaining[:TARGET_SIZE])]
        end = boundaries[-1] if boundaries else 0
        overlap = 0
        if len(boundaries) >= 2 and end >= MIN_CHUNK_SIZE and 30 <= end - boundaries[-2] <= 160:
            overlap = end - boundaries[-2]
        if end < MIN_CHUNK_SIZE:
            end = max(remaining.rfind(" ", MIN_CHUNK_SIZE, TARGET_SIZE), remaining.rfind("\n", MIN_CHUNK_SIZE, TARGET_SIZE))
        if end < MIN_CHUNK_SIZE:
            end = TARGET_SIZE
        pieces.append(remaining[:end].rstrip())
        remaining = remaining[end - overlap:].lstrip()
    if remaining:
        pieces.append(remaining)
    return pieces


def chunk_documents(docs: list[Document]) -> list[Chunk]:
    """Keep semantic sections separate and leave table rows intact."""
    chunks = []
    for document in docs:
        index = 0
        for heading, lines in _sections(document.text):
            current = ""
            for block in _blocks(lines):
                block_lines = block.splitlines()
                is_table = _table_at(block_lines, 0) is not None
                for piece in [block] if is_table else _split_paragraph(block):
                    if current and len(current) + 2 + len(piece) > TARGET_SIZE:
                        index += 1
                        chunks.append(Chunk(f"{document.path}:{index}", document.path, heading, current))
                        current = ""
                    current = current + "\n\n" + piece if current else piece
            if current:
                index += 1
                chunks.append(Chunk(f"{document.path}:{index}", document.path, heading, current))
    return chunks


def _tokens(text: str) -> list[str]:
    normalized = text.casefold().replace("ё", "е")
    tokens = [concept for concept, pattern in CONCEPT_PHRASES.items() if pattern.search(normalized)]
    if EVALUATION_INPUTS.search(normalized):
        tokens.append("evaluation_inputs")
    for token in WORD.findall(normalized):
        if token in STOP_WORDS or len(token) == 1 and not token.isdigit():
            continue
        alias = ALIASES.get(token)
        if alias is not None:
            tokens.append(alias)
        for concept, prefixes in CONCEPT_PREFIXES.items():
            if token.startswith(prefixes):
                tokens.append(concept)
        if len(token) >= 5 and "а" <= token[0] <= "я" and VOWEL.search(token):
            stem = VERB_ENDING.sub("", token)
            if stem == token:
                stem = RUSSIAN_ENDING.sub("", token)
            if len(stem) >= 3:
                token = stem
        if alias != token:
            tokens.append(token)
    return tokens


def _categories(text: str) -> set[int]:
    return {int(match[1] or match[2]) for match in CATEGORY.finditer(text)} | {
        CATEGORY_NUMBERS[match[1]] for match in CATEGORY_WORDS.finditer(text)
    }


def build_index(chunks: list[Chunk]) -> SearchIndex:
    lengths = []
    postings: dict[str, list[tuple[int, float]]] = {}
    for pos, chunk in enumerate(chunks):
        body = Counter(_tokens(chunk.text))
        headings = chunk.heading.split(" / ")
        section = Counter(_tokens(headings[-1]))
        ancestors = Counter(_tokens(" ".join(headings[:-1])))
        lengths.append(sum(body.values()) + sum(section.values()))
        for token in body.keys() | section.keys() | ancestors.keys():
            weight = body[token] + 1.3 * section[token] + 0.25 * ancestors[token]
            postings.setdefault(token, []).append((pos, weight))
    average = (sum(lengths) / len(lengths) or 1) if lengths else 1
    penalties = tuple(1.2 * (0.35 + 0.65 * length / average) for length in lengths)
    idf = {token: math.log1p((len(chunks) - len(items) + 0.5) / (len(items) + 0.5)) for token, items in postings.items()}
    return SearchIndex(tuple(chunks), {token: tuple(items) for token, items in postings.items()}, idf, penalties)


def retrieve(index: SearchIndex, query: Query, top_k: int = 12) -> list[Evidence]:
    """Retrieve actual lexical matches, with current-turn terms carrying more weight."""
    focus = query.focus or query.latest
    latest = set(_tokens(focus))
    terms = {token: 0.45 if token in CONTEXT_TERMS else 1.0 for token in sorted(latest)}
    for token in _tokens(query.search_text):
        terms.setdefault(token, 0.70 if token in CONCEPT_PHRASES else 0.50)
    if PHOTO_SUBMISSION.search(query.search_text.casefold()):
        terms.setdefault("photo_reading", 0.70)
    scores: dict[int, float] = {}
    for token, query_weight in terms.items():
        if token not in index.postings:
            continue
        # A standalone number or unit needs a topical term to be useful evidence.
        if token[0].isdigit() or token in {"квт", "мвт", "квтч", "квт·ч"}:
            continue
        for pos, frequency in index.postings[token]:
            scores[pos] = scores.get(pos, 0.0) + query_weight * index.idf[token] * frequency * 2.2 / (
                frequency + index.length_penalties[pos]
            )
    if not scores or top_k <= 0:
        return []
    for token in sorted(latest):
        if token[0].isdigit() or token in {"квт", "мвт", "квтч", "квт·ч"}:
            for pos, _ in index.postings.get(token, ()):
                if pos in scores:
                    scores[pos] += 0.35 * index.idf[token]
    # Generic follow-up language (price, availability, next step) has a narrower
    # meaning when the same question already names a subject in an earlier turn.
    anchors = CONCEPT_PHRASES.keys() & latest or CONCEPT_PHRASES.keys() & terms.keys()
    matching_anchor = {pos for token in anchors for pos, _ in index.postings.get(token, ())}
    if matching_anchor:
        for pos in scores:
            if pos not in matching_anchor:
                scores[pos] *= 0.42
    current = focus.casefold().replace("ё", "е")
    search = query.search_text.casefold().replace("ё", "е")
    facets = [pattern for pattern in FACETS.values() if pattern.search(current)]
    categories = _categories(current)
    if not categories and "категори" in query.history.casefold():
        # The previous assistant's question can supply the meaning of an
        # ordinal. The value itself comes from the client's current turn.
        ordinal = re.search(r"\b" + CATEGORY_ORDINAL + r"\b", current)
        if ordinal:
            categories = {CATEGORY_NUMBERS[ordinal[1]]}
    estimate = "metering_platform" in terms and METERING_ESTIMATE.search(current)
    sim_connection = bool(re.search(r"\bsim\b|\bсимк", current))
    siting = FACETS["solar_siting"].search(current)
    retail_metering = "site_metering" in latest and "солнечн" not in current
    for pos in scores:
        chunk = index.chunks[pos]
        heading = chunk.heading.casefold().replace("ё", "е")
        body = chunk.text.casefold().replace("ё", "е")
        if estimate:
            if heading.startswith("облачная платформа учета") or "для платформы учета" in heading:
                scores[pos] *= 2.4
            else:
                scores[pos] *= 0.40
        if sim_connection and "распознавание" in heading:
            scores[pos] *= 0.35
        if retail_metering and ("солнечн" in heading or "микрогенераци" in heading or "оптов" in heading):
            scores[pos] *= 0.35
        if siting and ("применяет ли мтс" in heading or "оборудование и гарантии" in heading):
            scores[pos] *= 0.35
        for pattern in facets:
            if pattern.search(heading):
                scores[pos] *= 1.55
            elif pattern.search(body):
                scores[pos] *= 1.25
        if categories:
            rows = {CATEGORY_NUMBERS[match[1]] for match in CATEGORY_ROW.finditer(body)}
            if categories & rows:
                scores[pos] *= 2.6
            elif categories & _categories(heading):
                scores[pos] *= 1.4
            elif _categories(heading):
                scores[pos] *= 0.55
    ordered = sorted(scores, key=lambda pos: (-scores[pos], pos))
    floor = scores[ordered[0]] * 0.25
    preferred = []
    if estimate:
        preferred.extend(
            next((pos for pos in ordered if re.search(pattern, index.chunks[pos].heading.casefold().replace("ё", "е"))), None)
            for pattern in (r"облачная платформа учета.*сколько это стоит", r"для платформы учета")
        )
    if PHOTO_SUBMISSION.search(search) and "tenant_metering" in terms:
        for pattern in (PHOTO_VERIFICATION, re.compile(r"как продукты связаны")):
            preferred.append(next(
                (pos for pos in ordered if pattern.search(index.chunks[pos].heading.casefold())),
                None,
            ))
    for rule, section in EVIDENCE_RULES:
        if not rule.search(search):
            continue
        match = next(
            (pos for pos in ordered if section.search(index.chunks[pos].heading.casefold().replace("ё", "е"))),
            None,
        )
        preferred.append(match)
    if any(pos is not None and "требуются ли персональные данные" in index.chunks[pos].heading.casefold()
           for pos in preferred):
        preferred.extend(
            pos for pos in ordered if "требуются ли персональные данные" in index.chunks[pos].heading.casefold()
        )
    # Keep the strongest lexical hit, then include distinct evidence for the
    # requested conditions before filling remaining positions by BM25.
    selected = dict.fromkeys([ordered[0], *(pos for pos in preferred if pos is not None),
                              *(pos for pos in ordered if scores[pos] >= floor)])
    return [
        Evidence(index.chunks[pos], rank)
        for rank, pos in enumerate(list(selected)[:top_k], start=1)
    ]
