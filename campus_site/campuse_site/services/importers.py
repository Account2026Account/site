# -*- coding: utf-8 -*-
"""Импорт недельного расписания (DOCX) и замен/кабинетов (PDF)."""
import re
from datetime import date
from werkzeug.security import generate_password_hash

try:
    from config import WD_RU, WD_SHORT
except ImportError:
    WD_RU = {"понедельник": 0, "вторник": 1, "среда": 2, "четверг": 3, "пятница": 4, "суббота": 5, "воскресенье": 6}
    WD_SHORT = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5}

def _norm_space(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

def _parse_room(text):
    """Парсит аудитории: 'ауд.335,351' / 'ауд. 101' / 'каб. 2-15' -> '335, 351'."""
    m = re.search(r"(?:ауд\.?|каб\.?)\s*([0-9A-Za-zА-Яа-я.,/\-\s]*)", text or "", re.I)
    if not m:
        return ""
    raw = _norm_space(m.group(1)).rstrip(".,;")
    if not raw or set(raw) <= {"-", "—", "–", "."}:
        return ""
    parts = [p.strip() for p in re.split(r"[,;]+", raw) if p.strip() and not set(p.strip()) <= {"-", "—", "–", "."}]
    return ", ".join(parts)

def _title_case_subject(name):
    """Нормализация названия предмета: ALL CAPS / mixed -> обычный вид с заглавной."""
    name = _norm_space(name)
    if not name:
        return name
    letters = [c for c in name if c.isalpha()]
    if not letters:
        return name
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    # Если почти всё заглавными — приводим к виду «Первое слово с заглавной»
    if upper_ratio >= 0.7:
        low = name.lower()
        # Сохраняем аббревиатуры из 2–4 букв в верхнем регистре (ИС, БЖД, ООП)
        parts = []
        for w in low.split():
            if re.fullmatch(r"[а-яёa-z]{2,4}", w) and w.upper() in (
                "ИС", "ИТ", "БЖД", "ООП", "БД", "ПО", "ОС", "ПК", "СУБД", "HTML", "CSS", "SQL", "XML", "API"
            ):
                parts.append(w.upper())
            else:
                parts.append(w[:1].upper() + w[1:] if w else w)
        return " ".join(parts)
    # Уже нормальный регистр — только убрать лишние пробелы
    return name


def _split_subj_teacher(cell):
    """'Русский язык Буртасова И.Л.' / несколько преподов / 'отмена' -> (subj, teacher, room, is_empty, is_cancel).
    teacher — строка со всеми преподавателями через запятую; subject без ФИО."""
    t = _norm_space(cell)
    if not t or set(t) <= {"-", "—", "–", "."} or t.lower() in ("-------", "—", "-"):
        return None, None, "", True, False
    if "отмен" in t.lower() or t.lower() in ("отм", "отмена"):
        return None, None, "", False, True
    room = _parse_room(t)
    t2 = re.sub(r"(?:ауд\.?|каб\.?)\s*[0-9A-Za-zА-Яа-я.,/\-\s]*", "", t, flags=re.I)
    t2 = re.sub(r"[\s,;]*[—–\-]+\s*$", "", t2).strip(" ,;")
    # «Мартынова-\nФидельман» / «Мартынова- Фидельман» -> «Мартынова-Фидельман»
    t2 = re.sub(r"\s*-\s*", "-", t2)
    t2 = re.sub(r"\s*,\s*", ", ", t2)
    # Одно ФИО: «Фамилия И.О.» или «Фамилия-Фамилия И.О.»
    one_teacher = r"[А-ЯЁA-Z][а-яёa-z]*(?:-[А-ЯЁA-Z][а-яёa-z]*)*\s+[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]\."
    teacher_pat = re.compile(one_teacher)
    matches = list(teacher_pat.finditer(t2))
    if matches:
        # непрерывный хвост совпадений (через пробел или запятую)
        teachers = []
        end = len(t2)
        for m in reversed(matches):
            gap = t2[m.end():end]
            if gap.strip() and not re.fullmatch(r"[\s,;]*", gap):
                break
            teachers.append(_norm_space(m.group(0).replace(" .", ".")))
            end = m.start()
        teachers.reverse()
        # нормализуем инициалы: «Е. Ю.» -> «Е.Ю.»
        def _norm_fio(s):
            s = re.sub(r"\s+", " ", s).strip()
            s = re.sub(r"([А-ЯЁA-Z])\.\s*([А-ЯЁA-Z])\.", r"\1.\2.", s)
            return s
        teachers = [_norm_fio(t) for t in teachers]
        teacher = ", ".join(teachers) if teachers else None
        subj = _norm_space(t2[:end]) if teachers else _norm_space(t2)
    else:
        # fallback: хвост «Фамилия И.О» / два последних токена
        parts = t2.split()
        if len(parts) >= 2 and re.match(r"^[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]\.?$", parts[-1]):
            teacher = " ".join(parts[-2:])
            subj = " ".join(parts[:-2]) if len(parts) > 2 else ""
        elif len(parts) >= 2 and re.match(r"^[А-ЯЁA-Z]", parts[-1]):
            teacher = " ".join(parts[-2:])
            subj = " ".join(parts[:-2]) if len(parts) > 2 else parts[0]
        else:
            teacher, subj = "", t2
    subj = _title_case_subject(subj) if subj else None
    return subj or None, teacher or None, room, False, False

def _norm_group_name(name):
    """Унификация имени группы: пробелы, тире, латиница→кириллица (C→С и т.п.)."""
    name = _norm_space(name).upper().replace(" ", "").replace("–", "-").replace("—", "-")
    trans = str.maketrans({
        "C": "С", "A": "А", "B": "В", "E": "Е", "H": "Н", "K": "К",
        "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    })
    return name.translate(trans)

def ensure_group(d, name):
    name = _norm_group_name(name)
    if not name:
        return None
    r = d.execute("SELECT id FROM groups WHERE name=?", (name,)).fetchone()
    if r:
        return r["id"]
    # мягкий поиск: без учёта регистра / уже нормализованные
    for g in d.execute("SELECT id, name FROM groups").fetchall():
        if _norm_group_name(g["name"]) == name:
            return g["id"]
    return d.execute("INSERT INTO groups(name) VALUES(?)", (name,)).lastrowid

def ensure_subject(d, name):
    name = _title_case_subject(_norm_space(name))
    if not name:
        return None
    r = d.execute("SELECT id FROM subjects WHERE name=?", (name,)).fetchone()
    if r:
        return r["id"]
    # case-insensitive match — при необходимости обновляем регистр
    r = d.execute("SELECT id, name FROM subjects").fetchall()
    for x in r:
        if x["name"].lower() == name.lower():
            if x["name"] != name and x["name"].isupper():
                d.execute("UPDATE subjects SET name=? WHERE id=?", (name, x["id"]))
            return x["id"]
    return d.execute("INSERT INTO subjects(name) VALUES(?)", (name,)).lastrowid

def ensure_teacher(d, name):
    name = _norm_space(name)
    if not name:
        return None
    # primary teacher = first in comma-separated list; остальные создаются отдельно при необходимости
    primary = name.split(",")[0].strip()
    others = [x.strip() for x in name.split(",")[1:] if x.strip()]

    def _find_or_create(nm):
        if not nm:
            return None
        r = d.execute("SELECT id FROM users WHERE role='teacher' AND name=?", (nm,)).fetchone()
        if r:
            return r["id"]
        # частичное совпадение: «Иванов И.И.» == «Иванов Иван Иванович» по фамилии+инициалам
        fam = nm.split()[0].lower() if nm.split() else ""
        inits = re.findall(r"[А-ЯЁA-Z]\.", nm)
        r = d.execute("SELECT id, name FROM users WHERE role='teacher'").fetchall()
        for x in r:
            xn = x["name"]
            if xn.lower() == nm.lower():
                return x["id"]
            xf = xn.split()[0].lower() if xn.split() else ""
            if fam and xf == fam:
                if not inits:
                    return x["id"]
                xi = re.findall(r"[А-ЯЁA-Z]\.", xn)
                if xi and all(a[0].upper() == b[0].upper() for a, b in zip(inits, xi)):
                    return x["id"]
                # «Иванов И.И.» vs «Иванов»
                if len(xn.split()) == 1:
                    return x["id"]
        base = re.sub(r"[^a-z0-9]", "", nm.lower().replace("ё", "e"))[:12] or "t"
        login = base
        n = 1
        while d.execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
            n += 1
            login = f"{base}{n}"
        pw = generate_password_hash("teacher123")
        return d.execute("INSERT INTO users(login,pw,name,role) VALUES(?,?,?,'teacher')",
                         (login, pw, nm)).lastrowid

    tid = _find_or_create(primary)
    # остальные преподы — тоже в БД (для назначений вызывающий код добавит)
    for o in others:
        _find_or_create(o)
    return tid

def cleanup_bogus_bells(d):
    """Удаляет пары с номером >12 или временем 00:00–00:00 (артефакты импорта кабинетов)."""
    bad = d.execute("""SELECT id FROM bells WHERE num>12 OR num<1
                       OR (start='00:00' AND end='00:00')""").fetchall()
    for b in bad:
        # не трогаем schedule, если есть — только «пустые» фантомы без занятий
        used = d.execute("SELECT 1 FROM schedule WHERE bell_id=? LIMIT 1", (b["id"],)).fetchone()
        if not used:
            d.execute("DELETE FROM bells WHERE id=?", (b["id"],))
    # если у пары 1..12 время 00:00 — сбросить на дефолт
    defaults = {1:("09:00","10:35"),2:("10:55","12:30"),3:("13:10","14:45"),
                4:("14:55","16:30"),5:("16:40","18:15"),6:("18:25","20:00")}
    for b in d.execute("SELECT id,num,start,end FROM bells").fetchall():
        if b["start"]=="00:00" and b["end"]=="00:00" and b["num"] in defaults:
            s,e = defaults[b["num"]]
            d.execute("UPDATE bells SET start=?,end=? WHERE id=?", (s,e,b["id"]))
    d.commit()

def ensure_bell(d, num, start=None, end=None):
    try:
        num = int(num)
    except (TypeError, ValueError):
        return None
    if num < 1 or num > 12:
        return None  # не создаём «214 пару» из номера кабинета
    r = d.execute("SELECT id, start, end FROM bells WHERE num=?", (num,)).fetchone()
    if r:
        if start and end and start != "00:00" and end != "00:00" and (r["start"] != start or r["end"] != end):
            # не перезаписываем нормальное время нулями
            d.execute("UPDATE bells SET start=?, end=? WHERE id=?", (start, end, r["id"]))
        return r["id"]
    start = start if start and start != "00:00" else "09:00"
    end = end if end and end != "00:00" else "10:35"
    return d.execute("INSERT INTO bells(num,start,end) VALUES(?,?,?)", (num, start, end)).lastrowid

def parse_time_range(s):
    m = re.search(r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})", s or "")
    if m:
        return m.group(1), m.group(2)
    return None, None


def _migrate_day_entries_to_changes(d):
    """Разовые schedule(day=...) без weekday сливаем в changes постоянного слота (group+bell+weekday).
    Нужно, когда сначала загрузили замены (создали day-записи), а потом — недельное расписание."""
    day_rows = d.execute("""SELECT * FROM schedule WHERE day IS NOT NULL AND day != '' AND (weekday IS NULL)""").fetchall()
    for s in day_rows:
        try:
            wd = date.fromisoformat(s["day"]).weekday()
        except Exception:
            continue
        weekly = d.execute(
            """SELECT id FROM schedule WHERE group_id=? AND bell_id=? AND weekday=? AND (day IS NULL OR day='')
               ORDER BY id LIMIT 1""",
            (s["group_id"], s["bell_id"], wd)).fetchone()
        if not weekly:
            # привяжем weekday к разовой, чтобы она участвовала как шаблон на этот день недели
            d.execute("UPDATE schedule SET weekday=? WHERE id=?", (wd, s["id"]))
            continue
        # переносим существующие changes со старого id на weekly
        for ch in d.execute("SELECT * FROM changes WHERE schedule_id=?", (s["id"],)).fetchall():
            d.execute("""INSERT INTO changes(schedule_id,day,kind,teacher_id,room,note) VALUES(?,?,?,?,?,?)
              ON CONFLICT(schedule_id,day) DO UPDATE SET kind=excluded.kind, teacher_id=excluded.teacher_id,
                room=excluded.room, note=excluded.note""",
                      (weekly["id"], ch["day"], ch["kind"], ch["teacher_id"], ch["room"] or "", ch["note"] or ""))
        # сама day-запись становится заменой на этот день
        d.execute("""INSERT INTO changes(schedule_id,day,kind,teacher_id,room,note) VALUES(?,?,?,?,?,?)
          ON CONFLICT(schedule_id,day) DO UPDATE SET kind='replace', teacher_id=excluded.teacher_id,
            room=excluded.room, note=excluded.note""",
                  (weekly["id"], s["day"], "replace", s["teacher_id"], s["room"] or "", ""))
        d.execute("DELETE FROM schedule WHERE id=?", (s["id"],))


def import_weekly_docx(d, path):
    """Импорт недельного расписания из DOCX (таблицы по группам)."""
    from docx import Document
    doc = Document(path)
    stats = {"groups": 0, "lessons": 0, "created_groups": [], "created_subjects": [], "created_teachers": []}
    # collect paragraphs + tables in order
    # python-docx doesn't give mixed order easily; process by matching group headers then following table
    paras = [p.text.strip() for p in doc.paragraphs]
    tables = doc.tables
    # map: for each table index, find preceding group header
    group_re = re.compile(r"РАСПИСАНИЕ\s+ГРУПП[АЫИ]\s+([0-9A-Za-zА-Яа-я\-]+)", re.I)
    headers = []
    for i, p in enumerate(paras):
        m = group_re.search(p)
        if m:
            headers.append((i, m.group(1).strip()))
    # assign tables to groups: sequential
    if not headers or not tables:
        return stats
    # If more tables than headers, pair by index
    for ti, table in enumerate(tables):
        if ti >= len(headers):
            break
        gname = headers[ti][1]
        # normalize group name like 11-А-26
        gname = gname.upper().replace(" ", "")
        before_g = d.execute("SELECT id FROM groups WHERE name=?", (gname,)).fetchone()
        gid = ensure_group(d, gname)
        if not before_g:
            stats["created_groups"].append(gname)
        stats["groups"] += 1
        # clear weekly (weekday-based) schedule for this group
        d.execute("DELETE FROM schedule WHERE group_id=? AND weekday IS NOT NULL AND (day IS NULL OR day='')", (gid,))
        # убрать «гибриды» day+weekday — они давали дубли пар на конкретную дату
        d.execute("DELETE FROM schedule WHERE group_id=? AND weekday IS NOT NULL AND day IS NOT NULL AND day!=''", (gid,))
        # parse header row for weekdays
        rows = table.rows
        if len(rows) < 2:
            continue
        head = [_norm_space(c.text).lower() for c in rows[0].cells]
        # find day columns
        day_cols = {}
        for ci, h in enumerate(head):
            for k, v in WD_RU.items():
                if k in h:
                    day_cols[ci] = v
            for k, v in WD_SHORT.items():
                if h == k or h.startswith(k):
                    day_cols[ci] = v
        pair_col = 0
        time_col = 1 if len(head) > 1 else None
        for ri in range(1, len(rows)):
            cells = [_norm_space(c.text) for c in rows[ri].cells]
            if not cells:
                continue
            # номер пары: только 1..12 (не путать с кабинетом 214)
            raw_pair = cells[pair_col] if pair_col < len(cells) else ""
            m_pair = re.search(r"(?:^|\b)([1-9]|1[0-2])\s*(?:пара)?\b", raw_pair, re.I)
            if not m_pair:
                m_pair = re.search(r"^\s*([1-9]|1[0-2])\s*$", raw_pair)
            if not m_pair:
                continue
            pnum = int(m_pair.group(1))
            start, end = (None, None)
            if time_col is not None and time_col < len(cells):
                start, end = parse_time_range(cells[time_col])
            # не затираем звонки нулями
            if start == "00:00" and end == "00:00":
                start, end = None, None
            bid = ensure_bell(d, pnum, start, end)
            if not bid:
                continue
            for ci, wd in day_cols.items():
                if ci >= len(cells):
                    continue
                subj, teacher, room, empty, cancel = _split_subj_teacher(cells[ci])
                if empty or cancel or not subj:
                    continue
                before_s = d.execute("SELECT id FROM subjects WHERE name=?", (subj,)).fetchone()
                sid = ensure_subject(d, subj)
                if not before_s:
                    stats["created_subjects"].append(subj)
                tid = ensure_teacher(d, teacher) if teacher else None
                if not tid:
                    tid = ensure_teacher(d, "Не назначен")
                # Все преподаватели из ячейки — в назначения (не только первый)
                if teacher:
                    for part in [p.strip() for p in teacher.split(",") if p.strip()]:
                        ot = ensure_teacher(d, part)
                        if ot:
                            d.execute("INSERT OR IGNORE INTO assignments(teacher_id,group_id,subject_id) VALUES(?,?,?)",
                                      (ot, gid, sid))
                extra_t = ""
                if teacher and "," in teacher:
                    parts = [p.strip() for p in teacher.split(",") if p.strip()]
                    if len(parts) > 1:
                        extra_t = ", ".join(parts[1:])
                d.execute("""INSERT INTO schedule(group_id,subject_id,teacher_id,bell_id,weekday,day,room,parity,teachers_extra)
                  VALUES(?,?,?,?,?,NULL,?,0,?)""", (gid, sid, tid, bid, wd, room or "", extra_t))
                d.execute("INSERT OR IGNORE INTO assignments(teacher_id,group_id,subject_id) VALUES(?,?,?)",
                          (tid, gid, sid))
                stats["lessons"] += 1
    # После загрузки недели: разовые записи (day=...), созданные ранее из замен без базы,
    # переносим в changes к соответствующему постоянному слоту, чтобы замены не «прилипали» к дневному расписанию.
    try:
        _migrate_day_entries_to_changes(d)
    except Exception:
        pass
    d.commit()
    return stats

def import_replacements_pdf(d, path, day_iso=None):
    """Импорт замен из PDF «Изменения в расписании на ...».
    Колонки: № | Группа | Исходная дисциплина+преподаватель | Замена (дисциплина+преподаватель) | Кабинет.
    """
    import pdfplumber
    stats = {"replacements": 0, "cancels": 0, "day": day_iso, "unmatched": 0}
    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        if not day_iso:
            months = {"января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
                      "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12}
            m2 = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
            m3 = re.search(r'"(\d{1,2})"\s+(\w+)\s+(\d{4})', text)
            m = re.search(r'на\s+"?(\d{1,2})"?\s*(\w+)\s*(\d{4})', text, re.I)
            if m2:
                day_iso = f"{m2.group(3)}-{int(m2.group(2)):02d}-{int(m2.group(1)):02d}"
            elif m3 and m3.group(2).lower() in months:
                day_iso = f"{m3.group(3)}-{months[m3.group(2).lower()]:02d}-{int(m3.group(1)):02d}"
            elif m and m.group(2).lower() in months:
                day_iso = f"{m.group(3)}-{months[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
        stats["day"] = day_iso
        if not day_iso:
            return stats
        wd = date.fromisoformat(day_iso).weekday()
        # повторная загрузка замен на тот же день — сначала снимаем старые замены/отмены на этот день
        d.execute("DELETE FROM changes WHERE day=?", (day_iso,))
        # разовые слоты, созданные прошлым импортом замен на этот день, тоже убираем
        d.execute("DELETE FROM schedule WHERE day=? AND (weekday IS NULL OR weekday='')", (day_iso,))

        def _cell(c):
            # pdfplumber часто даёт переносы строк внутри ячейки
            return _norm_space((c or "").replace("\n", " ").replace("\r", " "))

        for page in pdf.pages:
            tables = page.extract_tables() or []
            for table in tables:
                if not table or len(table) < 2:
                    continue
                for row in table[1:]:
                    if not row or len(row) < 3:
                        continue
                    cells = [_cell(c) for c in row]
                    try:
                        pnum = int(re.search(r"\d+", cells[0] or "").group())
                    except Exception:
                        continue
                    gname = (cells[1] or "").upper().replace(" ", "").replace("–", "-").replace("—", "-")
                    # латиница C/S ↔ кириллица С для групп вроде 14-С
                    gname = gname.replace("C", "С").replace("A", "А").replace("B", "В").replace("E", "Е").replace("H", "Н").replace("K", "К").replace("M", "М").replace("O", "О").replace("P", "Р").replace("T", "Т").replace("X", "Х")
                    if not gname or not re.search(r"\d", gname):
                        continue
                    if not (1 <= pnum <= 12):
                        continue
                    gid = ensure_group(d, gname)
                    orig = cells[2] if len(cells) > 2 else ""
                    repl = cells[3] if len(cells) > 3 else ""
                    room_cell = cells[4] if len(cells) > 4 else ""
                    room = _norm_space(room_cell)
                    if room.lower() in ("отм", "отмена") or set(room) <= {"-", "—", "–", "."}:
                        room = ""

                    bell = d.execute("SELECT id FROM bells WHERE num=?", (pnum,)).fetchone()
                    bell_id = bell["id"] if bell else ensure_bell(d, pnum)
                    if not bell_id:
                        continue

                    # Постоянный слот (без day) предпочтительнее разовой записи на этот день
                    cands = d.execute("""SELECT s.*, sub.name sname FROM schedule s
                        JOIN subjects sub ON sub.id=s.subject_id
                        WHERE s.group_id=? AND s.bell_id=?
                          AND (s.weekday=? OR s.day=?)
                        ORDER BY CASE WHEN s.day IS NULL OR s.day='' THEN 0 ELSE 1 END, s.id""",
                        (gid, bell_id, wd, day_iso)).fetchall()
                    sch = None
                    if cands:
                        # если есть исходная дисциплина — попробуем сопоставить по названию
                        orig_subj, _, _, _, _ = _split_subj_teacher(orig) if orig else (None, None, "", True, False)
                        if orig_subj:
                            ol = orig_subj.lower()
                            for c in cands:
                                sn = (c["sname"] or "").lower()
                                if sn == ol or ol in sn or sn in ol:
                                    sch = c
                                    break
                        if not sch:
                            sch = cands[0]

                    is_cancel = bool(re.search(r"отмен", (repl or ""), re.I)) or (repl or "").strip().lower() in ("отм", "отмена", "—", "-", "–")
                    if is_cancel:
                        if sch:
                            d.execute("""INSERT INTO changes(schedule_id,day,kind,teacher_id,room,note) VALUES(?,?,?,?,?,?)
                              ON CONFLICT(schedule_id,day) DO UPDATE SET kind='cancel', teacher_id=NULL, room='', note='отмена'""",
                                      (sch["id"], day_iso, "cancel", None, "", "отмена"))
                            stats["cancels"] += 1
                        else:
                            stats["unmatched"] += 1
                        continue

                    subj, teacher, room2, empty, cancel = _split_subj_teacher(repl or "")
                    if empty or cancel:
                        if cancel and sch:
                            d.execute("""INSERT INTO changes(schedule_id,day,kind,teacher_id,room,note) VALUES(?,?,?,?,?,?)
                              ON CONFLICT(schedule_id,day) DO UPDATE SET kind='cancel', teacher_id=NULL, room='', note='отмена'""",
                                      (sch["id"], day_iso, "cancel", None, "", "отмена"))
                            stats["cancels"] += 1
                        continue
                    if room2 and not room:
                        room = room2

                    tid = ensure_teacher(d, teacher) if teacher else None
                    # все преподы из замены
                    if teacher:
                        for part in [p.strip() for p in teacher.split(",") if p.strip()]:
                            ensure_teacher(d, part)

                    # note: «Новая дисциплина · Преподаватели» — для отображения
                    note_bits = []
                    if subj:
                        note_bits.append(subj)
                    if teacher:
                        note_bits.append(teacher)
                    note = " · ".join(note_bits) if note_bits else (repl or "")[:200]

                    if sch:
                        d.execute("""INSERT INTO changes(schedule_id,day,kind,teacher_id,room,note) VALUES(?,?,?,?,?,?)
                          ON CONFLICT(schedule_id,day) DO UPDATE SET kind='replace', teacher_id=excluded.teacher_id,
                            room=excluded.room, note=excluded.note""",
                                  (sch["id"], day_iso, "replace", tid, room or "", note[:300]))
                        if tid:
                            d.execute("INSERT OR IGNORE INTO assignments(teacher_id,group_id,subject_id) VALUES(?,?,?)",
                                      (tid, gid, sch["subject_id"]))
                        if teacher:
                            for part in [p.strip() for p in teacher.split(",") if p.strip()]:
                                ot = ensure_teacher(d, part)
                                if ot:
                                    d.execute("INSERT OR IGNORE INTO assignments(teacher_id,group_id,subject_id) VALUES(?,?,?)",
                                              (ot, gid, sch["subject_id"]))
                        stats["replacements"] += 1
                    else:
                        # нет слота в постоянном расписании (напр. «Дополнительное занятие») —
                        # создаём разовую запись ТОЛЬКО на этот день (weekday=NULL), не путаем с неделей
                        if subj:
                            sid = ensure_subject(d, subj)
                            tid = tid or ensure_teacher(d, "Не назначен")
                            extra_t = ""
                            if teacher and "," in teacher:
                                parts = [p.strip() for p in teacher.split(",") if p.strip()]
                                if len(parts) > 1:
                                    extra_t = ", ".join(parts[1:])
                            if teacher:
                                for part in [p.strip() for p in teacher.split(",") if p.strip()]:
                                    ot = ensure_teacher(d, part)
                                    if ot:
                                        d.execute("INSERT OR IGNORE INTO assignments(teacher_id,group_id,subject_id) VALUES(?,?,?)",
                                                  (ot, gid, sid))
                            # не создаём дубликат на тот же день+пару
                            exist = d.execute("""SELECT id FROM schedule WHERE group_id=? AND bell_id=? AND day=?""",
                                              (gid, bell_id, day_iso)).fetchone()
                            if exist:
                                d.execute("""UPDATE schedule SET subject_id=?, teacher_id=?, room=?, teachers_extra=?
                                  WHERE id=?""", (sid, tid, room or "", extra_t, exist["id"]))
                            else:
                                d.execute("""INSERT INTO schedule(group_id,subject_id,teacher_id,bell_id,weekday,day,room,parity,teachers_extra)
                                  VALUES(?,?,?,?,NULL,?,?,0,?)""", (gid, sid, tid, bell_id, day_iso, room or "", extra_t))
                            d.execute("INSERT OR IGNORE INTO assignments(teacher_id,group_id,subject_id) VALUES(?,?,?)",
                                      (tid, gid, sid))
                            stats["replacements"] += 1
                        else:
                            stats["unmatched"] += 1
    d.commit()
    return stats


def import_rooms_pdf(d, path, day_iso=None):
    """Импорт кабинетов (матрица группа × пара). Создаёт все группы из файла.
    Обновляет room в schedule/changes; если занятия ещё нет — только создаёт группы."""
    import pdfplumber
    stats = {"rooms": 0, "groups_created": 0, "groups_seen": 0, "day": day_iso, "cancels": 0}
    group_re = re.compile(r"^([0-9]{1,3}[-–]?[А-ЯA-Zа-яa-z]{1,3}[-–]?[0-9]{2,4}|[0-9]{2,3}-[А-ЯA-Z]{1,3}-[0-9]{2})", re.I)

    def norm_g(s):
        s = _norm_space(s).upper().replace(" ", "").replace("–", "-")
        return s

    def apply_room(gid, pnum, room, day_iso, wd):
        # номер пары только 1..12 — иначе из PDF попадают номера кабинетов (214, 451…)
        try:
            pnum = int(pnum)
        except (TypeError, ValueError):
            return False
        if pnum < 1 or pnum > 12:
            return False
        bell = d.execute("SELECT id FROM bells WHERE num=?", (pnum,)).fetchone()
        if not bell:
            bell_id = ensure_bell(d, pnum)
        else:
            bell_id = bell["id"]
        room = _norm_space(room)
        is_cancel = room.lower() in ("отм", "отмена")
        sch = d.execute(
            """SELECT id, room FROM schedule WHERE group_id=? AND bell_id=?
               AND ((weekday=? AND (day IS NULL OR day='')) OR day=?)""",
            (gid, bell_id, wd, day_iso)).fetchone()
        if not sch:
            return False
        if is_cancel:
            d.execute("""INSERT INTO changes(schedule_id,day,kind,teacher_id,room,note) VALUES(?,?,?,?,?,?)
              ON CONFLICT(schedule_id,day) DO UPDATE SET kind='cancel', note='отмена', room=''""",
                      (sch["id"], day_iso, "cancel", None, "", "отмена"))
            stats["cancels"] += 1
        else:
            # только кабинет: kind='ok' — НЕ замена. Не затираем уже стоящую replace/cancel.
            d.execute("""INSERT INTO changes(schedule_id,day,kind,teacher_id,room,note) VALUES(?,?,?,?,?,?)
              ON CONFLICT(schedule_id,day) DO UPDATE SET
                room=excluded.room,
                kind=CASE
                  WHEN changes.kind IN ('cancel','replace') THEN changes.kind
                  ELSE 'ok'
                END""",
                      (sch["id"], day_iso, "ok", None, room, ""))
            # если кабинет совпал с недельным — можно не хранить change, но ok достаточно
            stats["rooms"] += 1
        return True

    with pdfplumber.open(path) as pdf:
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        if not day_iso:
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", full_text)
            if m:
                y = int(m.group(3))
                if y < 100:
                    y += 2000
                day_iso = f"{y}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            else:
                m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", full_text)
                if m:
                    y = int(m.group(3))
                    if y < 100:
                        y += 2000
                    day_iso = f"{y}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        stats["day"] = day_iso
        if not day_iso:
            return stats
        wd = date.fromisoformat(day_iso).weekday()

        # 1) try structured tables
        # parsed tables flag (unused, kept for structure)
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for table in tables:
                if not table or len(table) < 2:
                    continue
                head = [_norm_space(c) for c in table[0]]
                pair_cols = {}
                for ci, h in enumerate(head):
                    m = re.match(r"^(\d+)$", h or "")
                    if m:
                        pair_cols[ci] = int(m.group(1))
                # sometimes header is second row
                if not pair_cols and len(table) > 1:
                    head2 = [_norm_space(c) for c in table[1]]
                    for ci, h in enumerate(head2):
                        m = re.match(r"^(\d+)$", h or "")
                        if m:
                            pair_cols[ci] = int(m.group(1))
                    data_start = 2
                else:
                    data_start = 1
                if not pair_cols:
                    continue
                for row in table[data_start:]:
                    cells = [_norm_space(c) for c in (row or [])]
                    if not cells:
                        continue
                    gname = norm_g(cells[0] or "")
                    if not gname or not re.search(r"\d", gname):
                        # try find group-like token in first cells
                        for c in cells[:2]:
                            g2 = norm_g(c or "")
                            if group_re.match(g2):
                                gname = g2
                                break
                    if not gname or not re.search(r"\d", gname):
                        continue
                    before = d.execute("SELECT id FROM groups WHERE name=?", (gname,)).fetchone()
                    gid = ensure_group(d, gname)
                    stats["groups_seen"] += 1
                    if not before:
                        stats["groups_created"] += 1
                    for ci, pnum in pair_cols.items():
                        if ci >= len(cells):
                            continue
                        room = cells[ci]
                        if not room or room in ("?", "-", "—"):
                            continue
                        apply_room(gid, pnum, room, day_iso, wd)

        # 2) text-line fallback: "11-А-26 430 404 204 204"
        if stats["rooms"] == 0 and stats["groups_created"] == 0:
            for line in full_text.splitlines():
                line = _norm_space(line)
                m = group_re.match(line.replace(" ", ""))
                # better: start of line is group
                parts = line.split()
                if not parts:
                    continue
                gname = norm_g(parts[0])
                if not group_re.match(gname) and not re.match(r"^\d{1,3}-[А-ЯA-Z]{1,3}-\d{2}$", gname):
                    # try parts[0] with cyrillic
                    if not re.search(r"\d", parts[0]):
                        continue
                    gname = norm_g(parts[0])
                    if not re.search(r"[А-ЯA-Z]", gname):
                        continue
                before = d.execute("SELECT id FROM groups WHERE name=?", (gname,)).fetchone()
                gid = ensure_group(d, gname)
                stats["groups_seen"] += 1
                if not before:
                    stats["groups_created"] += 1
                # remaining tokens as rooms for pairs 1..n
                rooms = parts[1:]
                for i, room in enumerate(rooms, 1):
                    if not room or room in ("?", "-", "—"):
                        continue
                    if room.lower() in ("отм", "отмена") or re.match(r"^[\d,./А-ЯA-Za-z]+$", room):
                        apply_room(gid, i, room, day_iso, wd)

        # 3) always harvest all group-like tokens from text
        for m in re.finditer(r"\b(\d{1,3}-[А-ЯA-Zа-я]{1,3}-\d{2,4})\b", full_text, re.I):
            gname = norm_g(m.group(1))
            before = d.execute("SELECT id FROM groups WHERE name=?", (gname,)).fetchone()
            ensure_group(d, gname)
            if not before:
                stats["groups_created"] += 1
                stats["groups_seen"] += 1

    cleanup_bogus_bells(d)
    d.commit()
    return stats
