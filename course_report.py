import os
import json
import re
import csv
import io
import uuid
import zlib
from datetime import datetime
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, session, flash, Response

app = Flask(__name__, template_folder='course_report_templates', static_folder='public')
app.secret_key = 'super_secret_key_for_course_report'
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'temp_uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

def load_courses():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'courses_config.json')
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f).get('courses', [])
    return []

def get_available_courses():
    courses = load_courses()
    custom_courses = session.get('custom_courses', [])
    existing_names = {course.get('name') for course in courses}
    for course in custom_courses:
        if course.get('name') and course.get('name') not in existing_names:
            courses.append(course)
            existing_names.add(course.get('name'))
    return courses

def get_course_clos(course_name):
    courses = get_available_courses()
    return next((c.get('clos', []) for c in courses if c.get('name') == course_name), [])

def group_clos_by_domain(clos):
    grouped = {
        'knowledge': [],
        'skills': [],
        'values': [],
        'other': []
    }
    for clo in clos or []:
        clo_text = str(clo).strip()
        if clo_text.startswith('1.'):
            grouped['knowledge'].append(clo_text)
        elif clo_text.startswith('2.'):
            grouped['skills'].append(clo_text)
        elif clo_text.startswith('3.'):
            grouped['values'].append(clo_text)
        elif clo_text:
            grouped['other'].append(clo_text)
    return grouped

def decode_pdf_string(value, cmap=None):
    value = value.replace(r'\\', '\u0000')
    value = value.replace(r'\(', '(').replace(r'\)', ')')
    value = value.replace(r'\n', ' ').replace(r'\r', ' ').replace(r'\t', ' ')

    def replace_octal(match):
        try:
            return chr(int(match.group(1), 8))
        except ValueError:
            return ''

    value = re.sub(r'\\([0-7]{1,3})', replace_octal, value)
    value = value.replace('\u0000', '\\')
    if cmap:
        return ''.join(cmap.get(ord(char), char) for char in value)
    return value

def decode_pdf_hex_string(value, cmap=None):
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return ''

    if cmap:
        return ''.join(cmap.get(byte, chr(byte)) for byte in raw)
    if raw.startswith(b'\xfe\xff'):
        return raw[2:].decode('utf-16-be', errors='ignore')
    return raw.decode('latin-1', errors='ignore')

def decode_pdf_text_array(array_content, cmap=None):
    tokens = re.finditer(
        r'\((?:\\.|[^\\()])*\)|<([0-9A-Fa-f\s]+)>|[-+]?\d*\.?\d+',
        array_content,
        flags=re.S
    )
    text = ''
    for token_match in tokens:
        token = token_match.group(0)
        if token.startswith('('):
            text += decode_pdf_string(token[1:-1], cmap)
        elif token.startswith('<'):
            text += decode_pdf_hex_string(token[1:-1], cmap)
        else:
            try:
                spacing_adjustment = abs(float(token))
            except ValueError:
                continue
            if spacing_adjustment > 120 and text and not text.endswith(' '):
                text += ' '
    return text

def extract_pdf_block_text(block, cmap=None):
    text = ''
    for text_match in re.finditer(
        r'\[(.*?)\]\s*TJ|\((.*?)\)\s*Tj|<([0-9A-Fa-f\s]+)>\s*Tj',
        block,
        flags=re.S
    ):
        if text_match.group(1) is not None:
            text += decode_pdf_text_array(text_match.group(1), cmap)
        elif text_match.group(2) is not None:
            text += decode_pdf_string(text_match.group(2), cmap)
        elif text_match.group(3) is not None:
            text += decode_pdf_hex_string(text_match.group(3), cmap)
    return text

def estimate_pdf_text_width(text, font_size):
    width = 0.0
    for char in text:
        if char.isspace():
            width += font_size * 0.28
        elif char in 'il.,;:\'!|':
            width += font_size * 0.25
        elif char in 'mwMW@#%':
            width += font_size * 0.75
        else:
            width += font_size * 0.50
    return width

def join_pdf_text_segments(segments):
    output = []
    previous = None
    for segment in segments:
        text = segment['text']
        if not text:
            continue
        if previous is None:
            output.append(text.strip())
            previous = segment
            continue

        same_line = (
            previous.get('x') is not None
            and segment.get('x') is not None
            and abs((segment.get('y') or 0) - (previous.get('y') or 0)) < 2.0
        )
        if same_line:
            previous_text = output[-1]
            separator = '' if previous_text.endswith(' ') or text.startswith(' ') else ' '
            output[-1] = f"{previous_text}{separator}{text.strip()}"
        else:
            output.append(text.strip())
        previous = segment
    return '\n'.join(part for part in output if part)

def decode_cmap_hex(value):
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return ''
    if len(raw) % 2 == 0:
        return raw.decode('utf-16-be', errors='ignore')
    return raw.decode('latin-1', errors='ignore')

def parse_tounicode_cmap(cmap_text):
    cmap = {}
    for src, dst in re.findall(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', cmap_text):
        if len(src) <= 2:
            cmap[int(src, 16)] = decode_cmap_hex(dst)

    for src_start, src_end, dst_start in re.findall(
        r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>',
        cmap_text
    ):
        if len(src_start) > 2 or len(src_end) > 2:
            continue
        start = int(src_start, 16)
        end = int(src_end, 16)
        dst = int(dst_start, 16)
        for offset, code in enumerate(range(start, end + 1)):
            cmap[code] = chr(dst + offset)

    return cmap

def decode_pdf_stream_object(object_body):
    stream_match = re.search(rb'stream\r?\n(.*?)\r?\nendstream', object_body, flags=re.S)
    if not stream_match:
        return b''
    stream = stream_match.group(1)
    dictionary = object_body[:stream_match.start()]
    if b'FlateDecode' in dictionary:
        try:
            return zlib.decompress(stream)
        except zlib.error:
            return b''
    return stream

def build_pdf_font_cmaps(pdf_bytes):
    objects = {
        int(match.group(1)): match.group(2)
        for match in re.finditer(rb'(\d+)\s+0\s+obj(.*?)endobj', pdf_bytes, flags=re.S)
    }
    tounicode_maps = {}
    for object_id, body in objects.items():
        stream = decode_pdf_stream_object(body)
        if b'begincmap' in stream:
            cmap_text = stream.decode('latin-1', errors='ignore')
            tounicode_maps[object_id] = parse_tounicode_cmap(cmap_text)

    font_object_cmaps = {}
    for object_id, body in objects.items():
        match = re.search(rb'/ToUnicode\s+(\d+)\s+0\s+R', body)
        if match:
            cmap = tounicode_maps.get(int(match.group(1)))
            if cmap:
                font_object_cmaps[object_id] = cmap

    font_resource_cmaps = {}
    for body in objects.values():
        font_block_match = re.search(rb'/Font\s*<<(.*?)>>', body, flags=re.S)
        if not font_block_match:
            continue
        for name, object_ref in re.findall(rb'/([A-Za-z0-9]+)\s+(\d+)\s+0\s+R', font_block_match.group(1)):
            cmap = font_object_cmaps.get(int(object_ref))
            if cmap:
                font_resource_cmaps[name.decode('ascii', errors='ignore')] = cmap
    return font_resource_cmaps

def extract_text_from_pdf_streams(pdf_bytes):
    extracted = []
    font_cmaps = build_pdf_font_cmaps(pdf_bytes)
    for match in re.finditer(rb'stream\r?\n(.*?)\r?\nendstream', pdf_bytes, flags=re.S):
        stream = match.group(1)
        dictionary = pdf_bytes[max(0, match.start() - 700):match.start()]
        if b'FlateDecode' in dictionary:
            try:
                stream = zlib.decompress(stream)
            except zlib.error:
                continue

        content = stream.decode('latin-1', errors='ignore')
        segments = []
        for block_match in re.finditer(r'BT(.*?)ET', content, flags=re.S):
            block = block_match.group(1)
            font_name_match = re.findall(r'/([A-Za-z0-9]+)\s+[0-9.]+\s+Tf', block)
            font_name = font_name_match[-1] if font_name_match else ''
            cmap = font_cmaps.get(font_name)
            block_text = extract_pdf_block_text(block, cmap)
            if not block_text.strip():
                continue

            font_match = re.findall(r'/[A-Za-z0-9]+\s+([0-9.]+)\s+Tf', block)
            font_size = float(font_match[-1]) if font_match else 10.0
            matrix_match = re.findall(
                r'[-+]?[0-9.]+\s+[-+]?[0-9.]+\s+[-+]?[0-9.]+\s+[-+]?[0-9.]+\s+([-+]?[0-9.]+)\s+([-+]?[0-9.]+)\s+Tm',
                block
            )
            x = y = None
            if matrix_match:
                x = float(matrix_match[-1][0])
                y = float(matrix_match[-1][1])

            segments.append({
                'text': block_text,
                'x': x,
                'y': y,
                'font_size': font_size,
                'end_x': (x + estimate_pdf_text_width(block_text, font_size)) if x is not None else None
            })

        if segments:
            extracted.append(join_pdf_text_segments(segments))

    return '\n'.join(extracted)

def extract_pdf_text(filepath):
    try:
        import pypdf
        reader = pypdf.PdfReader(filepath)
        return '\n'.join(page.extract_text() or '' for page in reader.pages)
    except Exception:
        pass

    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(filepath)
        return '\n'.join(page.extract_text() or '' for page in reader.pages)
    except Exception:
        pass

    with open(filepath, 'rb') as f:
        return extract_text_from_pdf_streams(f.read())

def compact_text(text):
    return re.sub(r'\s+', ' ', text or '').strip()

def extract_first_int(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue
    return None

def infer_course_report_metrics(text):
    normalized = compact_text(text)
    question_numbers = set()

    question_patterns = [
        r'\bQ(?:uestion)?\s*[-#:]?\s*(\d{1,3})\b',
        r'\bQuestion\s+No\.?\s*(\d{1,3})\b',
        r'\bItem\s*[-#:]?\s*(\d{1,3})\b'
    ]
    for pattern in question_patterns:
        for match in re.finditer(pattern, normalized, flags=re.I):
            number = int(match.group(1))
            if 0 < number <= 200:
                question_numbers.add(number)

    total_questions = extract_first_int([
        r'(?:number|no\.?|total)\s+of\s+questions?\D{0,30}(\d{1,3})',
        r'questions?\s*(?:count|total|number)?\s*[:=]\s*(\d{1,3})',
        r'(\d{1,3})\s+questions?\b'
    ], normalized)

    if question_numbers:
        total_questions = max(total_questions or 0, max(question_numbers))

    total_students = extract_first_int([
        r'(?:number|no\.?|total)\s+of\s+students?\D{0,30}(\d{1,4})',
        r'students?\s*(?:count|total|number)?\s*[:=]\s*(\d{1,4})',
        r'(\d{1,4})\s+students?\b'
    ], normalized)

    questions = []
    if total_questions:
        questions = [f'Q{i}' for i in range(1, total_questions + 1)]
    elif question_numbers:
        questions = [f'Q{i}' for i in sorted(question_numbers)]

    confidence = 'High' if questions and total_students else 'Medium' if questions or total_students else 'Low'
    return {
        'questions': questions,
        'total_questions': len(questions) if questions else (total_questions or 0),
        'total_students': total_students or 0,
        'confidence': confidence,
        'text_sample': normalized[:1200]
    }

def value_after_label(lines, labels):
    for index, line in enumerate(lines):
        for label in labels:
            pattern = rf'(?i)\b{re.escape(label)}\b\s*[:\-]?\s*(.+)$'
            match = re.search(pattern, line)
            if match and match.group(1).strip():
                value = match.group(1).strip()
                if len(value) > 1:
                    return value
            if re.search(rf'(?i)\b{re.escape(label)}\b\s*$', line) and index + 1 < len(lines):
                next_value = lines[index + 1].strip()
                if next_value:
                    return next_value
    return ''

def clean_clo_text(value):
    value = re.sub(r'\s+', ' ', value or '').strip()
    value = re.sub(r'\s+(Teaching\s+Strategies|Assessment\s+Methods|Code\s+of\s+PLOs|Domain)\b.*$', '', value, flags=re.I).strip()
    value = re.sub(r'\bdat\s+a\b|\bda\s+ta\b', 'data', value, flags=re.I)
    value = re.sub(r'\bsucg\b', 'such', value, flags=re.I)
    value = re.sub(r'\bevalute\b', 'evaluate', value, flags=re.I)
    value = re.sub(r'\s+-\s+', '-', value)
    value = re.sub(r'\band\s+along\s+with\b', 'along with', value, flags=re.I)
    value = re.sub(r'\bdesign\s+create\b', 'Design/Create', value, flags=re.I)
    value = re.sub(r'\bai\b', 'AI', value, flags=re.I)
    value = re.sub(r'\s+([,.;:])', r'\1', value)
    if value:
        value = value[0].upper() + value[1:]
    return value

def flexible_label(label):
    parts = []
    for char in label:
        if char.isspace():
            parts.append(r'\s+')
        else:
            parts.append(re.escape(char) + r'\s*')
    return ''.join(parts)

COURSE_SPEC_WORDS = {
    'a', 'an', 'and', 'application', 'apply', 'appropriate', 'algorithms', 'as', 'assignment',
    'assignments', 'basic', 'big', 'brainstorming', 'class', 'collaborate', 'course', 'data', 'deletion',
    'develop', 'different', 'discussion', 'ended', 'evaluate', 'exam', 'exams', 'given', 'homework',
    'identify', 'implementation', 'in', 'insertion', 'knowledge', 'labs', 'learning', 'lectures',
    'of', 'on', 'open', 'outcomes', 'problem', 'problems', 'program', 'programming', 'quizzes',
    'relation', 'rely', 'require', 'requires', 'responsibility', 'searching', 'skills', 'solve',
    'solving', 'sorting', 'strategies', 'strengths', 'structures', 'such', 'teams', 'that', 'the',
    'to', 'types', 'understanding', 'values', 'weaknesses', 'with',
    'abilities', 'about', 'addressing', 'AI', 'ai', 'analyse', 'analytical', 'applications',
    'assess', 'assessments', 'capability', 'complex', 'computational', 'computer',
    'considerations', 'critical', 'datasets', 'decision', 'developments', 'discuss', 'driven',
    'ethical', 'evaluating', 'examine', 'expertise', 'fields', 'for', 'frameworks', 'from', 'ideas',
    'improve', 'including', 'influence', 'issues', 'machine', 'making', 'manage', 'management', 'methods',
    'multiple', 'new', 'practical', 'predictive', 'privacy', 'procedures', 'purposes',
    'reasoning', 'recent', 'recognize', 'robust', 'science', 'sophisticated', 'special',
    'specialized', 'statistical', 'statistics', 'tackle', 'tech', 'techniques', 'through',
    'tools', 'topics', 'trends', 'understand', 'utilize',
    'align', 'alignment', 'along', 'another', 'choice', 'classify', 'compare', 'concepts',
    'create', 'demonstrate', 'design', 'effectively', 'explain', 'justify', 'levels',
    'managing', 'maturity', 'metadata', 'one', 'open', 'practices', 'proficiency',
    'over', 'quality', 'reuse', 'sharing', 'solution', 'solutions', 'specify', 'standards', 'strategy', 'variety'
}

COURSE_SPEC_ALIASES = {
    'evalute': 'evaluate',
    'sucg': 'such'
}

def segment_compact_words(value):
    compact = re.sub(r'[^A-Za-z]', '', value or '').lower()
    if not compact:
        return ''

    max_word_length = 18
    dp = [None] * (len(compact) + 1)
    dp[0] = (0, [])
    for start in range(len(compact)):
        if dp[start] is None:
            continue
        for end in range(start + 1, min(len(compact), start + max_word_length) + 1):
            raw_word = compact[start:end]
            word = COURSE_SPEC_ALIASES.get(raw_word, raw_word)
            if word not in COURSE_SPEC_WORDS:
                continue
            score = dp[start][0] + len(raw_word) ** 2
            if dp[end] is None or score > dp[end][0]:
                dp[end] = (score, dp[start][1] + [word])

    if dp[-1] is None:
        return ''
    return ' '.join(dp[-1][1])

def title_case_course_name(value):
    lowered_words = {'and', 'in', 'of', 'for', 'to', 'with'}
    titled = []
    for index, word in enumerate(str(value or '').split()):
        lower = word.lower()
        if index > 0 and lower in lowered_words:
            titled.append(lower)
        else:
            titled.append(lower[:1].upper() + lower[1:])
    return ' '.join(titled)

def repair_remaining_pdf_fragments(value):
    def replace_match(match):
        fragment = match.group(0)
        segmented = segment_compact_words(fragment)
        return segmented if segmented else fragment

    value = re.sub(r'\b(?:[A-Za-z]{1,4}\s+){2,}[A-Za-z]{1,6}\b', replace_match, value)
    value = re.sub(r'\b([A-Za-z]+men)\s+t\b', lambda m: m.group(1) + 't', value, flags=re.I)
    value = re.sub(r'\b([A-Za-z]+ica)\s+l\b', lambda m: m.group(1) + 'l', value, flags=re.I)
    value = re.sub(
        r'\b(algorithm|application|concept|dataset|format|level|method|practice|procedure|solution|structure|system)\s+s\b',
        lambda m: m.group(1) + 's',
        value,
        flags=re.I
    )
    value = re.sub(r'\b([A-Za-z]{3,})\s+([bcdefghjklmnopqrstuvwxyz])\b(?=\s*(?:[.,;:]|$))', lambda m: m.group(1) + m.group(2), value)
    value = re.sub(
        r'\b([A-Za-z]{2,})\s+(ated|ence|hms|rstanding|edictive|soning|ment|tion|sion|tions|sions)\b',
        lambda m: m.group(1) + m.group(2),
        value,
        flags=re.I
    )
    value = re.sub(r'\b([B-HJ-Zb-hj-km-ru-z])\s+([a-z]{3,})\b', lambda m: m.group(1) + m.group(2), value)
    value = re.sub(r'\bdat\s+a\b|\bda\s+ta\b', 'data', value, flags=re.I)
    value = re.sub(r'\ba\s+lgorithm(s?)\b', r'algorithm\1', value, flags=re.I)
    value = re.sub(r'\balgorithm\s+s\b', 'algorithms', value, flags=re.I)
    value = re.sub(r'\bs\s+tandards\b', 'standards', value, flags=re.I)
    return value

def clean_pdf_fragment(value):
    value = re.sub(r'[\x00-\x1f]+', ' ', value or '')
    value = re.sub(r'\\', ' ', value)
    value = re.sub(r'\s*&\s*', ' and ', value)
    value = re.sub(r'[/:;]+', ' ', value)
    value = re.sub(r'\s{3,}', '  ', value).strip()
    groups = re.split(r'\s{2,}', value)
    cleaned_groups = []
    stopwords = {'of', 'in', 'to', 'on', 'as', 'is', 'be', 'or', 'and', 'the', 'for', 'with', 'that'}

    for group in groups:
        tokens = [token for token in group.split() if token]
        small_token_ratio = sum(1 for token in tokens if len(token.strip('.,;:-')) <= 3) / len(tokens) if tokens else 0
        segmented = segment_compact_words(group) if len(tokens) >= 2 and small_token_ratio > 0.4 else ''
        if segmented:
            suffix = ''
            if re.search(r'\.\s*$', group):
                suffix = '.'
            elif re.search(r',\s*$', group):
                suffix = ','
            cleaned_groups.append(segmented + suffix)
        else:
            cleaned_groups.append(group)

    cleaned = re.sub(r'\s+', ' ', ' '.join(cleaned_groups)).strip()
    return repair_remaining_pdf_fragments(cleaned)

def extract_course_spec_section(raw_text, start_label, end_label):
    start_matches = list(re.finditer(flexible_label(start_label), raw_text, flags=re.I | re.S))
    end_matches = list(re.finditer(flexible_label(end_label), raw_text, flags=re.I | re.S))
    candidates = []
    for start_match in start_matches:
        next_end = next((end_match for end_match in end_matches if end_match.start() > start_match.end()), None)
        if next_end:
            section = raw_text[start_match.end():next_end.start()]
            clo_ids = len(re.findall(r'\b[123]\s*\.\s*[1-9]\d*\b', section))
            candidates.append((clo_ids, len(section), section))
    if not candidates:
        return ''
    return max(candidates, key=lambda item: (item[0], item[1]))[2]

def extract_course_spec_metadata(text):
    lines = [compact_text(line) for line in (text or '').splitlines()]
    lines = [line for line in lines if line]
    raw_text = re.sub(r'[\x00-\x1f]+W\b', ' ', text or '')
    raw_text = re.sub(r'[\x00-\x1f]+', ' ', raw_text)
    normalized = compact_text(raw_text)

    course_name = value_after_label(lines, ['Course Name', 'Course Title', 'Course'])
    course_code = value_after_label(lines, ['Course Code', 'Course Number', 'Course ID', 'Course No'])

    title_match = re.search(
        rf'{flexible_label("Course Title")}\s*[:\-\u061b]?\s*(.+?)\s+{flexible_label("Course Code")}',
        raw_text,
        flags=re.I | re.S
    )
    if title_match:
        course_name = clean_pdf_fragment(title_match.group(1))
        if course_name.islower():
            course_name = title_case_course_name(course_name)

    code_match = re.search(
        rf'{flexible_label("Course Code")}\s*[:\-\u061b]?\s*(?:\S\s*){{0,12}}?([A-Z](?:\s*[A-Z]){{1,5}}\s*\d(?:\s*\d){{2,3}}[A-Z]?|[A-Z]{{2,5}}\s*\d{{3,4}}[A-Z]?)',
        raw_text,
        flags=re.I | re.S
    )
    if code_match:
        course_code = re.sub(r'\s+', '', code_match.group(1)).upper()

    if not course_code:
        code_match = re.search(r'\b([A-Z]{2,5}\s*\d{3,4}[A-Z]?)\b', normalized)
        if code_match:
            course_code = re.sub(r'\s+', '', code_match.group(1))

    if not course_name:
        title_match = re.search(r'(?i)(?:Course\s+(?:Name|Title)\s*[:\-]?\s*)(.{4,120}?)(?=\s+(?:Course\s+(?:Code|Number|ID)|Credit|Prerequisite|$))', normalized)
        if title_match:
            course_name = title_match.group(1).strip()

    clo_map = {}
    line_text = '\n'.join(lines)
    for match in re.finditer(r'(?m)^\s*((?:[123]\.\d+|CLO\s*\d+))\s+(.+)$', line_text, flags=re.I):
        clo_id = re.sub(r'\s+', '', match.group(1).upper())
        if re.match(r'^[123]\.0$', clo_id):
            continue
        clo_body = clean_clo_text(clean_pdf_fragment(match.group(2)))
        if clo_body and len(clo_body) > 8:
            clo_map[clo_id] = f"{clo_id} {clo_body}"

    section = extract_course_spec_section(raw_text, 'Course Learning Outcomes', 'Course Content')
    if section:
        for match in re.finditer(r'\b([123]\s*\.\s*[1-9]\d*)\s+(.+?)\s+\b([KSV]\s*\d+)\b', section, flags=re.I | re.S):
            clo_id = re.sub(r'\s+', '', match.group(1))
            clo_body = clean_clo_text(clean_pdf_fragment(match.group(2)))
            if clo_body and len(clo_body) > 8:
                clo_map[clo_id] = f"{clo_id} {clo_body}"

    if not clo_map:
        for match in re.finditer(r'\b([123]\.\d+)\s+(.{12,220}?)(?=\s+[123]\.\d+\s+|\s+CLO\s*\d+\s+|$)', normalized, flags=re.I):
            clo_id = match.group(1)
            if re.match(r'^[123]\.0$', clo_id):
                continue
            clo_body = clean_clo_text(clean_pdf_fragment(match.group(2)))
            if clo_body:
                clo_map[clo_id] = f"{clo_id} {clo_body}"

    clos = list(clo_map.values())
    display_name = course_name
    if course_code and course_name and course_code not in course_name:
        display_name = f"{course_name} ({course_code})"
    elif course_code and not course_name:
        display_name = course_code

    return {
        'name': display_name,
        'course_name': course_name,
        'course_code': course_code,
        'clos': clos,
        'grouped_clos': group_clos_by_domain(clos)
    }

def question_number_from_label(label):
    label = str(label).strip()
    patterns = [
        r'^Answers?\s*(\d{1,3})$',
        r'^Q(?:uestion)?\s*[-#:]?\s*(\d{1,3})$',
        r'^Item\s*[-#:]?\s*(\d{1,3})$'
    ]
    for pattern in patterns:
        match = re.search(pattern, label, flags=re.I)
        if match:
            return int(match.group(1))
    return None

def find_question_header_row(df):
    best = None
    for row_position, (_, row) in enumerate(df.iterrows()):
        question_cells = []
        for col_index, value in row.items():
            if pd.isna(value):
                continue
            number = question_number_from_label(value)
            if number:
                question_cells.append((col_index, number, str(value).strip()))
        if best is None or len(question_cells) > len(best['question_cells']):
            best = {'row_position': row_position, 'question_cells': question_cells}
    return best if best and len(best['question_cells']) >= 2 else None

def count_students_from_question_sheet(df, header_info):
    answer_columns = [col for col, _, _ in header_info['question_cells']]
    count = 0
    for _, row in df.iloc[header_info['row_position'] + 1:].iterrows():
        first_value = '' if pd.isna(row.iloc[0]) else str(row.iloc[0]).strip()
        if re.search(r'^(answer\s+key|mean|average|median|total)$', first_value, flags=re.I):
            continue
        populated_answers = sum(0 if pd.isna(row[col]) or str(row[col]).strip() == '' else 1 for col in answer_columns)
        if populated_answers > 0:
            count += 1
    return count

def infer_simple_table_metrics(df):
    clean_df = df.dropna(how='all')
    if clean_df.empty:
        return None

    header_info = find_question_header_row(clean_df)
    if header_info:
        questions = [f'Q{number}' for _, number, _ in sorted(header_info['question_cells'], key=lambda item: item[1])]
        return {
            'questions': questions,
            'total_questions': len(questions),
            'total_students': count_students_from_question_sheet(clean_df, header_info),
            'confidence': 'High',
            'text_sample': '',
            'max_scores': {question: 1.0 for question in questions}
        }

    df_with_headers = clean_df.copy()
    df_with_headers.columns = [str(col).strip() for col in df_with_headers.iloc[0]]
    df_with_headers = df_with_headers.iloc[1:].dropna(how='all')
    question_columns = []
    for col_position, col in enumerate(df_with_headers.columns):
        number = question_number_from_label(col)
        if number:
            question_columns.append((col_position, col, number))

    if question_columns:
        questions = [f'Q{number}' for _, _, number in sorted(question_columns, key=lambda item: item[2])]
        max_scores = {}
        for col_position, _, number in question_columns:
            values = pd.to_numeric(df_with_headers.iloc[:, col_position], errors='coerce').dropna()
            max_scores[f'Q{number}'] = float(values.max()) if not values.empty else 1.0
        return {
            'questions': questions,
            'total_questions': len(questions),
            'total_students': len(df_with_headers),
            'confidence': 'Medium',
            'text_sample': '',
            'max_scores': max_scores
        }

    numeric_df = df_with_headers.apply(pd.to_numeric, errors='coerce')
    numeric_positions = [
        col_position
        for col_position in range(numeric_df.shape[1])
        if not numeric_df.iloc[:, col_position].dropna().empty
    ]
    if numeric_positions:
        questions = [str(df_with_headers.columns[col_position]) for col_position in numeric_positions]
        max_scores = {}
        for col_position in numeric_positions:
            question = str(df_with_headers.columns[col_position])
            values = numeric_df.iloc[:, col_position].dropna()
            max_scores[question] = float(values.max()) if not values.empty else 1.0
        return {
            'questions': questions,
            'total_questions': len(questions),
            'total_students': len(df_with_headers),
            'confidence': 'Low',
            'text_sample': '',
            'max_scores': max_scores
        }
    return None

def infer_spreadsheet_metrics(filepath, file_ext):
    if file_ext == '.csv':
        df = pd.read_csv(filepath, header=None)
        metrics = infer_simple_table_metrics(df)
        if metrics:
            metrics['text_sample'] = f"Detected from CSV. Rows: {df.shape[0]}, columns: {df.shape[1]}."
            return metrics
    else:
        workbook = pd.ExcelFile(filepath)
        best_metrics = None
        best_sheet = None
        for sheet_name in workbook.sheet_names:
            df = pd.read_excel(filepath, sheet_name=sheet_name, header=None)
            metrics = infer_simple_table_metrics(df)
            if metrics and (best_metrics is None or metrics['total_questions'] > best_metrics['total_questions']):
                best_metrics = metrics
                best_sheet = sheet_name
        if best_metrics:
            best_metrics['text_sample'] = f"Detected from sheet: {best_sheet}. Questions were read from answer/question columns."
            return best_metrics

    return {
        'questions': [],
        'total_questions': 0,
        'total_students': 0,
        'confidence': 'Low',
        'text_sample': 'No spreadsheet question columns were detected.',
        'max_scores': {}
    }

def normalize_answer(value):
    if pd.isna(value):
        return ''
    text = str(value).strip().upper()
    return re.sub(r'\s+', '', text)

def build_scores_from_question_sheet(df, requested_questions):
    clean_df = df.dropna(how='all')
    if clean_df.empty:
        return None

    header_info = find_question_header_row(clean_df)
    if not header_info:
        return None

    requested = set(requested_questions)
    question_columns = {
        f'Q{number}': col_index
        for col_index, number, _ in header_info['question_cells']
        if f'Q{number}' in requested
    }
    if not question_columns:
        return None

    rows_after_header = clean_df.iloc[header_info['row_position'] + 1:]
    answer_key = None
    score_rows = []

    for _, row in rows_after_header.iterrows():
        first_value = '' if pd.isna(row.iloc[0]) else str(row.iloc[0]).strip()
        if re.search(r'^answer\s+key$', first_value, flags=re.I):
            answer_key = {
                question: normalize_answer(row[col_index])
                for question, col_index in question_columns.items()
            }
            continue
        if re.search(r'^(mean|average|median|total)$', first_value, flags=re.I):
            continue

        populated_answers = sum(
            0 if pd.isna(row[col_index]) or str(row[col_index]).strip() == '' else 1
            for col_index in question_columns.values()
        )
        if populated_answers == 0:
            continue

        if answer_key:
            score_rows.append({
                question: 1.0 if normalize_answer(row[col_index]) == answer_key.get(question, '') else 0.0
                for question, col_index in question_columns.items()
            })
        else:
            score_rows.append({
                question: pd.to_numeric(row[col_index], errors='coerce')
                for question, col_index in question_columns.items()
            })

    if not score_rows:
        return None
    return pd.DataFrame(score_rows), 'binary' if answer_key else 'numeric'

def build_score_dataframe(filepath, file_ext, requested_questions):
    requested_questions = list(requested_questions)

    if file_ext == '.csv':
        df = pd.read_csv(filepath)
        if all(question in df.columns for question in requested_questions):
            return df[requested_questions].apply(pd.to_numeric, errors='coerce').fillna(0), 'numeric'

        raw_df = pd.read_csv(filepath, header=None)
        scores = build_scores_from_question_sheet(raw_df, requested_questions)
        if scores:
            return scores
    else:
        df = pd.read_excel(filepath)
        if all(question in df.columns for question in requested_questions):
            return df[requested_questions].apply(pd.to_numeric, errors='coerce').fillna(0), 'numeric'

        workbook = pd.ExcelFile(filepath)
        for sheet_name in workbook.sheet_names:
            raw_df = pd.read_excel(filepath, sheet_name=sheet_name, header=None)
            scores = build_scores_from_question_sheet(raw_df, requested_questions)
            if scores:
                return scores

    return pd.DataFrame(columns=requested_questions), 'numeric'

def prefixed_question(assessment_name, question):
    return f"{assessment_name} {question}"

def combine_assessment_metrics(assessment_files):
    combined_questions = []
    combined_max_scores = {}
    total_students = 0
    notes = []
    student_counts = {}
    confidence = 'Low'

    for assessment in assessment_files:
        metrics = assessment.get('metrics', {})
        questions = metrics.get('questions') or []
        student_counts[assessment['label']] = metrics.get('total_students') or 0
        if questions and metrics.get('confidence') == 'High':
            confidence = 'High'
        elif questions and confidence != 'High':
            confidence = 'Medium'

        for question in questions:
            combined = prefixed_question(assessment['label'], question)
            combined_questions.append(combined)
            combined_max_scores[combined] = metrics.get('max_scores', {}).get(question, 1.0)

        total_students = max(total_students, metrics.get('total_students') or 0)
        if metrics.get('text_sample'):
            notes.append(f"{assessment['label']}: {metrics['text_sample']}")

    nonzero_counts = {label: count for label, count in student_counts.items() if count > 0}
    student_count_warning = ''
    if len(set(nonzero_counts.values())) > 1:
        count_text = ', '.join(f"{label}: {count}" for label, count in nonzero_counts.items())
        student_count_warning = f"Student count mismatch across uploaded files ({count_text}). One or more files may have missing students. You can continue, but results will use the matched rows available across the selected files."

    return {
        'questions': combined_questions,
        'total_questions': len(combined_questions),
        'total_students': total_students,
        'confidence': confidence,
        'text_sample': ' '.join(notes),
        'max_scores': combined_max_scores,
        'student_counts': student_counts,
        'student_count_warning': student_count_warning
    }

def build_combined_score_dataframe(assessment_files, requested_questions):
    frames = []
    for assessment in assessment_files:
        local_questions = []
        rename_map = {}
        for question in requested_questions:
            prefix = f"{assessment['label']} "
            if str(question).startswith(prefix):
                local_question = str(question)[len(prefix):]
                local_questions.append(local_question)
                rename_map[local_question] = question

        if not local_questions:
            continue

        filepath = os.path.join(app.config['UPLOAD_FOLDER'], assessment['stored_name'])
        score_df, _ = build_score_dataframe(filepath, assessment['ext'], local_questions)
        if score_df.empty:
            continue
        frames.append(score_df.rename(columns=rename_map))

    if not frames:
        return pd.DataFrame(columns=list(requested_questions)), 'binary'

    min_rows = min(len(frame) for frame in frames)
    normalized_frames = [frame.reset_index(drop=True).iloc[:min_rows] for frame in frames]
    return pd.concat(normalized_frames, axis=1).fillna(0), 'binary'

def calculate_clo_results():
    assessment_files = session.get('assessment_files') or []
    file_id = session.get('file_id')
    file_ext = session.get('file_ext')
    target_percentages = session.get('target_percentages', {"_global": 60.0})
    mapping_data = session.get('mapping', {})

    if not (assessment_files or file_id) or not mapping_data:
        return None, 0, "No mappings were provided."

    if assessment_files:
        score_df, score_mode = build_combined_score_dataframe(assessment_files, mapping_data.keys())
    else:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}{file_ext}")
        score_df, score_mode = build_score_dataframe(filepath, file_ext, mapping_data.keys())
    total_students = len(score_df)
    if total_students == 0:
        return None, 0, "Could not calculate scores from the uploaded file. Please check that the selected questions exist in the file."

    clo_stats = {}
    for col, data in mapping_data.items():
        for clo in data.get('clos', []):
            if clo not in clo_stats:
                clo_stats[clo] = {
                    'questions': [],
                    'students_achieved': 0,
                    'total_possible_score': 0
                }
            clo_stats[clo]['questions'].append(col)
            clo_stats[clo]['total_possible_score'] += data['max_score']

    for clo, stats in clo_stats.items():
        cols = stats['questions']
        max_possible = stats['total_possible_score']
        clo_target_pct = target_percentages.get(clo, target_percentages.get('_global', 60.0))
        target_score = max_possible * (clo_target_pct / 100.0)

        student_scores = pd.Series(0.0, index=score_df.index)
        for col in cols:
            if col not in score_df.columns:
                continue
            question_max = mapping_data.get(col, {}).get('max_score', 1.0)
            if score_mode == 'binary':
                student_scores = student_scores + (score_df[col].fillna(0).astype(float) * question_max)
            else:
                student_scores = student_scores + score_df[col].fillna(0).astype(float)

        achieved_count = (student_scores >= target_score).sum()
        stats['students_achieved'] = int(achieved_count)
        stats['achievement_percentage'] = round((achieved_count / total_students) * 100, 2) if total_students > 0 else 0
        stats['target_score'] = round(target_score, 2)
        stats['target_pct'] = round(clo_target_pct, 2)

    return clo_stats, total_students, None

def get_course_report_info():
    raw_name = session.get('course_name') or ''
    match = re.search(r'\(([^()]*)\)\s*$', raw_name)
    course_id = match.group(1).strip() if match else ''
    course_name = raw_name[:match.start()].strip() if match else raw_name.strip()
    return {
        'course_name': course_name or raw_name,
        'course_id': course_id,
        'raw_name': raw_name
    }

def format_question_label(question):
    question = str(question)
    match = re.match(r'^(.+?)\s+Q(\d+)$', question)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    match = re.match(r'^Q(\d+)$', question)
    if match:
        return f"Question {match.group(1)}"
    return question

def pdf_escape(value):
    return str(value).replace('\\', r'\\').replace('(', r'\(').replace(')', r'\)')

def get_jpeg_size(image_bytes):
    index = 2
    while index < len(image_bytes):
        if image_bytes[index] != 0xFF:
            index += 1
            continue
        marker = image_bytes[index + 1]
        index += 2
        if marker in (0xD8, 0xD9):
            continue
        if index + 2 > len(image_bytes):
            break
        segment_length = int.from_bytes(image_bytes[index:index + 2], 'big')
        if marker in range(0xC0, 0xC4) and index + 7 < len(image_bytes):
            height = int.from_bytes(image_bytes[index + 3:index + 5], 'big')
            width = int.from_bytes(image_bytes[index + 5:index + 7], 'big')
            return width, height
        index += segment_length
    return 500, 500

def wrap_pdf_text(value, max_chars=95):
    words = str(value).split()
    lines = []
    current = ''
    for word in words:
        if len(current) + len(word) + 1 > max_chars:
            if current:
                lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or ['']

def pdf_text(parts, x, y, text, size=10, font="F1"):
    safe_text = pdf_escape(str(text).encode('latin-1', errors='replace').decode('latin-1'))
    parts.append(f"BT /{font} {size} Tf {x} {y} Td ({safe_text}) Tj ET")

def pdf_line(parts, x1, y1, x2, y2):
    parts.append(f"{x1} {y1} m {x2} {y2} l S")

def pdf_rect(parts, x, y, width, height, fill=False):
    operator = "f" if fill else "S"
    parts.append(f"{x} {y} {width} {height} re {operator}")

def draw_pdf_lines(parts, lines, x, top_y, size=7, line_height=9, font="F1"):
    for index, line in enumerate(lines):
        pdf_text(parts, x, top_y - (index * line_height), line, size, font)

def build_results_pdf(stats, total_students, course_info):
    logo_path = os.path.join(app.static_folder, 'logo.jpg')
    logo_bytes = b''
    logo_width = 0
    logo_height = 0
    if os.path.exists(logo_path):
        with open(logo_path, 'rb') as f:
            logo_bytes = f.read()
        logo_width, logo_height = get_jpeg_size(logo_bytes)

    content_parts = []
    if logo_bytes:
        content_parts.append("q")
        content_parts.append("115 0 0 105 45 682 cm")
        content_parts.append("/Im1 Do")
        content_parts.append("Q")

    report_date = datetime.now().strftime("%Y-%m-%d")
    content_parts.append("0.102 0.396 0.420 RG")
    content_parts.append("0.102 0.396 0.420 rg")
    pdf_text(content_parts, 180, 750, "CLO Attainment Report", 17, "F2")
    content_parts.append("0.608 0.494 0.333 RG")
    pdf_line(content_parts, 180, 735, 560, 735)

    content_parts.append("0 0 0 RG")
    content_parts.append("0 0 0 rg")
    pdf_text(content_parts, 180, 710, f"Course Name: {course_info.get('course_name', '')}", 11, "F1")
    pdf_text(content_parts, 180, 692, f"Course ID: {course_info.get('course_id', '') or 'N/A'}", 11, "F1")
    pdf_text(content_parts, 180, 674, f"Report Date: {report_date}", 11, "F1")

    content_parts.append("0.965 0.973 0.980 rg")
    pdf_rect(content_parts, 50, 625, 510, 42, True)
    content_parts.append("0.835 0.855 0.890 RG")
    pdf_rect(content_parts, 50, 625, 510, 42, False)
    content_parts.append("0 0 0 rg")
    pdf_text(content_parts, 70, 650, "Total Students Evaluated", 10, "F1")
    pdf_text(content_parts, 70, 632, str(total_students), 16, "F2")
    pdf_text(content_parts, 245, 650, "Mapped CLOs", 10, "F1")
    pdf_text(content_parts, 245, 632, str(len(stats)), 16, "F2")

    page_contents = [content_parts]
    table_x = 30
    table_y = 590
    col_widths = [220, 125, 50, 55, 45, 65]
    headers = ["CLO", "Questions", "Max", "Target", "Achieved", "Achievement"]

    def draw_table_header(parts, y_position):
        parts.append("0.102 0.396 0.420 rg")
        pdf_rect(parts, table_x, y_position, sum(col_widths), 24, True)
        parts.append("1 1 1 rg")
        header_x = table_x + 5
        for header, width in zip(headers, col_widths):
            pdf_text(parts, header_x, y_position + 8, header, 8, "F2")
            header_x += width
        parts.append("0 0 0 rg")
        parts.append("0.835 0.855 0.890 RG")

    def new_continuation_page():
        parts = []
        parts.append("0.102 0.396 0.420 RG")
        parts.append("0.102 0.396 0.420 rg")
        pdf_text(parts, 40, 755, "CLO Attainment Report", 14, "F2")
        parts.append("0.608 0.494 0.333 RG")
        pdf_line(parts, 40, 740, 560, 740)
        parts.append("0 0 0 RG")
        parts.append("0 0 0 rg")
        pdf_text(parts, 40, 720, f"Course Name: {course_info.get('course_name', '')}", 9, "F1")
        pdf_text(parts, 330, 720, f"Report Date: {report_date}", 9, "F1")
        draw_table_header(parts, 680)
        page_contents.append(parts)
        return parts, 680

    draw_table_header(content_parts, table_y)
    y = table_y
    current_parts = content_parts
    for clo, data in stats.items():
        question_text = ", ".join(format_question_label(question) for question in data['questions'])
        clo_lines = wrap_pdf_text(clo, 48)
        question_lines = wrap_pdf_text(question_text, 26)
        line_count = max(len(clo_lines), len(question_lines), 2)
        row_height = max(36, 16 + (line_count * 9))

        if y - row_height < 55:
            current_parts, y = new_continuation_page()

        row_y = y - row_height
        pdf_rect(current_parts, table_x, row_y, sum(col_widths), row_height, False)
        x = table_x
        for width in col_widths[:-1]:
            x += width
            pdf_line(current_parts, x, row_y, x, row_y + row_height)

        text_top = row_y + row_height - 12
        draw_pdf_lines(current_parts, clo_lines, table_x + 5, text_top, 6.7, 9, "F1")
        draw_pdf_lines(current_parts, question_lines, table_x + 225, text_top, 6.7, 9, "F1")
        number_y = row_y + row_height - 21
        pdf_text(current_parts, table_x + 350, number_y, f"{data['total_possible_score']:.2f}", 7.5, "F1")
        pdf_text(current_parts, table_x + 400, number_y, f"{data['target_score']:.2f}", 7.5, "F1")
        pdf_text(current_parts, table_x + 455, number_y, str(data['students_achieved']), 7.5, "F1")
        pdf_text(current_parts, table_x + 500, number_y, f"{data['achievement_percentage']:.2f}%", 7.5, "F1")
        y = row_y

    for parts in page_contents:
        parts.append("0.5 0.5 0.5 rg")
        pdf_text(parts, 50, 35, "Generated by CLO Attainment Report Generator", 8, "F1")

    page_count = len(page_contents)
    font_regular_id = 3 + page_count
    font_bold_id = font_regular_id + 1
    content_start_id = font_bold_id + 1
    image_id = content_start_id + page_count if logo_bytes else None
    image_resource = f" /XObject << /Im1 {image_id} 0 R >>" if logo_bytes else ""
    page_kids = " ".join(f"{3 + index} 0 R" for index in range(page_count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{page_kids}] /Count {page_count} >>".encode('ascii')
    ]
    for page_index in range(page_count):
        content_id = content_start_id + page_index
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R >>{image_resource} >> /Contents {content_id} 0 R >>".encode('ascii')
        )
    objects.extend([
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"
    ])
    for parts in page_contents:
        stream = "\n".join(parts).encode('latin-1')
        objects.append(b"<< /Length " + str(len(stream)).encode('ascii') + b" >>\nstream\n" + stream + b"\nendstream")
    if logo_bytes:
        objects.append(
            f"<< /Type /XObject /Subtype /Image /Width {logo_width} /Height {logo_height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_bytes)} >>\nstream\n".encode('ascii')
            + logo_bytes
            + b"\nendstream"
        )

    pdf = io.BytesIO()
    pdf.write(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(pdf.tell())
        pdf.write(f"{idx} 0 obj\n".encode('ascii'))
        pdf.write(obj)
        pdf.write(b"\nendobj\n")
    xref_offset = pdf.tell()
    pdf.write(f"xref\n0 {len(objects) + 1}\n".encode('ascii'))
    pdf.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.write(f"{offset:010d} 00000 n \n".encode('ascii'))
    pdf.write(f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode('ascii'))
    return pdf.getvalue()

@app.route('/api/courses')
def api_courses():
    return json.dumps(get_available_courses())

@app.route('/course-specification', methods=['GET', 'POST'])
def course_specification():
    extracted = None
    if request.method == 'POST':
        if request.form.get('action') == 'add':
            course_name = request.form.get('course_name', '').strip()
            course_code = request.form.get('course_code', '').strip()
            try:
                clos = json.loads(request.form.get('clos_json', '[]'))
            except json.JSONDecodeError:
                clos = []

            clos = [clo.strip() for clo in clos if isinstance(clo, str) and clo.strip()]
            display_name = course_name
            if course_code and course_code not in display_name:
                display_name = f"{course_name} ({course_code})" if course_name else course_code

            if not display_name or not clos:
                flash("Please extract a valid course name and CLO list before adding the course.")
                return redirect(request.url)

            custom_courses = session.get('custom_courses', [])
            custom_courses = [course for course in custom_courses if course.get('name') != display_name]
            custom_courses.append({'name': display_name, 'clos': clos})
            session['custom_courses'] = custom_courses
            session['selected_course_name'] = display_name
            flash(f"Added course from specification: {display_name}")
            return redirect(url_for('index'))

        if 'course_spec_file' not in request.files:
            flash("Please upload a course specification PDF.")
            return redirect(request.url)

        file = request.files['course_spec_file']
        if not file or file.filename == '':
            flash("Please upload a course specification PDF.")
            return redirect(request.url)

        file_ext = os.path.splitext(file.filename)[1].lower()
        if file_ext != '.pdf':
            flash("Course specification must be uploaded as a PDF.")
            return redirect(request.url)

        file_id = str(uuid.uuid4())
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}{file_ext}")
        file.save(filepath)

        try:
            text = extract_pdf_text(filepath)
            extracted = extract_course_spec_metadata(text)
        except Exception as e:
            flash(f"Could not read course specification PDF: {e}")
            return redirect(request.url)

        if not extracted.get('name') or not extracted.get('clos'):
            flash("Could not fully extract the course name/code and CLOs. Please check the PDF text and try again.")
            return render_template('course_specification.html', extracted=extracted)

        flash("Review the extracted course information, then add it to the course list if it is correct.")
        return render_template('course_specification.html', extracted=extracted)

    return render_template('course_specification.html', extracted=extracted)

@app.route('/analyze-report', methods=['POST'])
def analyze_report():
    course_name = request.form.get('report_course_name')
    clos = get_course_clos(course_name)

    if 'report_file' not in request.files:
        flash("No course report file uploaded")
        return redirect(url_for('index'))

    file = request.files['report_file']
    if file.filename == '':
        flash("No selected course report file")
        return redirect(url_for('index'))

    file_ext = os.path.splitext(file.filename)[1].lower()
    allowed_exts = {'.pdf', '.csv', '.xlsx', '.xls'}
    if file_ext not in allowed_exts:
        flash("Please upload a PDF, CSV, or Excel course report.")
        return redirect(url_for('index'))

    file_id = str(uuid.uuid4())
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}{file_ext}")
    file.save(filepath)

    try:
        if file_ext == '.pdf':
            text = extract_pdf_text(filepath)
            metrics = infer_course_report_metrics(text)
        else:
            metrics = infer_spreadsheet_metrics(filepath, file_ext)
    except Exception as e:
        flash(f"Error reading course report file: {e}")
        return redirect(url_for('index'))

    if not metrics['questions']:
        flash("Could not detect question labels in the file. You can still enter the question count manually below.")

    return render_template(
        'report_detected.html',
        course_name=course_name,
        clos=clos,
        metrics=metrics,
        filename=file.filename
    )

@app.route('/manual-report', methods=['POST'])
def manual_report():
    course_name = request.form.get('manual_course_name')
    clos = get_course_clos(course_name)
    total_students = request.form.get('manual_students', type=int, default=0)
    total_questions = request.form.get('manual_questions', type=int, default=0)
    questions = [f'Q{i}' for i in range(1, max(total_questions, 0) + 1)]
    metrics = {
        'questions': questions,
        'total_questions': len(questions),
        'total_students': max(total_students, 0),
        'confidence': 'Manual',
        'text_sample': ''
    }
    return render_template(
        'report_detected.html',
        course_name=course_name,
        clos=clos,
        metrics=metrics,
        filename='Manual entry'
    )

@app.route('/save-question-clos', methods=['POST'])
def save_question_clos():
    mapped = []
    question_ids = set()
    for key in request.form.keys():
        if key.startswith('question_clo_'):
            question_ids.add(key.replace('question_clo_', ''))

    for question in sorted(question_ids, key=lambda item: int(item[1:]) if re.match(r'^Q\d+$', item) else item):
        clos = [clo for clo in request.form.getlist(f'question_clo_{question}') if clo and clo != 'IGNORE']
        if clos:
            mapped.append({'question': question, 'clos': clos})

    flash(f"Saved CLO selections for {len(mapped)} question(s).")
    return redirect(url_for('index'))

@app.route('/', methods=['GET', 'POST'])
def index():
    courses = get_available_courses()
    if request.method == 'POST':
        course_name = request.form.get('course_name')
        
        # Extract target percentages and edited CLO text
        target_percentages = {}
        custom_clos = []
        
        # Get the number of CLOs by finding indices in the form data
        indices = set()
        for key in request.form.keys():
            if key.startswith('clo_text_'):
                indices.add(key.replace('clo_text_', ''))
                
        for idx in sorted(list(indices), key=lambda x: int(x)):
            clo_text = request.form.get(f'clo_text_{idx}', '').strip()
            if clo_text:
                custom_clos.append(clo_text)
                try:
                    target_percentages[clo_text] = float(request.form.get(f'target_{idx}', 60.0))
                except ValueError:
                    target_percentages[clo_text] = 60.0 # Default
                    
        if not target_percentages:
            # Fallback if no specific CLO targets are provided
            global_target = request.form.get('target_percentage', type=float, default=60.0)
            target_percentages = {"_global": global_target}
        
        assessment_files = []

        upload_groups = [
            ('quiz_files', 'Quiz', True),
            ('assignment_files', 'Assignment', True),
            ('midterm_file', 'Midterm', False),
            ('final_file', 'Final', False),
            ('project_file', 'Project', False)
        ]

        for field_name, base_label, is_multiple in upload_groups:
            files = request.files.getlist(field_name) if is_multiple else [request.files.get(field_name)]
            uploaded_files = [file for file in files if file and file.filename]

            for index, file in enumerate(uploaded_files, start=1):
                label = f"{base_label} {index}" if is_multiple else base_label

                file_ext = os.path.splitext(file.filename)[1].lower()
                if file_ext not in {'.csv', '.xlsx', '.xls'}:
                    flash(f"Invalid {label} file format. Please upload CSV or Excel.")
                    return redirect(request.url)

                file_id = str(uuid.uuid4())
                stored_name = f"{file_id}{file_ext}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], stored_name)
                file.save(filepath)

                try:
                    metrics = infer_spreadsheet_metrics(filepath, file_ext)
                except Exception:
                    metrics = {
                        'questions': [],
                        'total_questions': 0,
                        'total_students': 0,
                        'confidence': 'Low',
                        'text_sample': '',
                        'max_scores': {}
                    }

                assessment_files.append({
                    'label': label,
                    'stored_name': stored_name,
                    'ext': file_ext,
                    'original_name': file.filename,
                    'metrics': metrics
                })

        if not assessment_files:
            flash("Please upload at least one Quiz, Assignment, Midterm, Final, or Project file.")
            return redirect(request.url)

        report_metrics = combine_assessment_metrics(assessment_files)
        session.pop('file_id', None)
        session.pop('file_ext', None)
        session['assessment_files'] = assessment_files
        session['course_name'] = course_name
        session['target_percentages'] = target_percentages
        session['custom_clos'] = custom_clos
        session['report_metrics'] = report_metrics
        session.pop('mapping', None)

        return redirect(url_for('mapping'))
            
    selected_course_name = session.pop('selected_course_name', '')
    return render_template('report_index.html', courses=courses, selected_course_name=selected_course_name)

@app.route('/mapping', methods=['GET', 'POST'])
def mapping():
    assessment_files = session.get('assessment_files') or []
    file_id = session.get('file_id')
    file_ext = session.get('file_ext')
    course_name = session.get('course_name')
    
    if not (assessment_files or file_id):
        return redirect(url_for('index'))

    report_metrics = session.get('report_metrics') or {}

    numeric_cols = []
    fallback_student_count = 0
    if not assessment_files:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}{file_ext}")
        try:
            if file_ext == '.csv':
                df = pd.read_csv(filepath)
            else:
                df = pd.read_excel(filepath)
        except Exception as e:
            flash(f"Error reading file: {e}")
            return redirect(url_for('index'))
        numeric_cols = df.select_dtypes(include=['number']).columns.tolist()
        fallback_student_count = len(df)

    detected_questions = report_metrics.get('questions') or []
    columns = detected_questions if detected_questions else numeric_cols
    total_students = report_metrics.get('total_students') or fallback_student_count
    total_questions = report_metrics.get('total_questions') or len(columns)
    max_scores = report_metrics.get('max_scores') or {}
    
    # Get CLOs for the selected course
    # Use custom edited CLOs if available, otherwise load from config
    course_clos = session.get('custom_clos')
    if not course_clos:
        course_clos = get_course_clos(course_name)

    if request.method == 'POST':
        mapping_data = {}
        missing_questions = []
        for col in columns:
            clos = [clo for clo in request.form.getlist(f"clo_{col}") if clo and clo != "IGNORE"]
            max_score_str = request.form.get(f"max_{col}")
            
            if not clos:
                missing_questions.append(format_question_label(col))
                continue

            try:
                max_score = float(max_score_str) if max_score_str else 1.0
            except ValueError:
                max_score = 1.0
            mapping_data[col] = {"clos": clos, "max_score": max_score}

        if missing_questions:
            flash(f"Please select at least one CLO for: {', '.join(missing_questions)}")
            session['mapping'] = mapping_data
            return redirect(url_for('mapping'))
                    
        session['mapping'] = mapping_data
        return redirect(url_for('results'))

    return render_template(
        'report_mapping.html',
        columns=columns,
        clos=course_clos,
        course_name=course_name,
        total_students=total_students,
        total_questions=total_questions,
        detection_confidence=report_metrics.get('confidence', 'Low'),
        detection_note=report_metrics.get('text_sample', ''),
        max_scores=max_scores,
        existing_mapping=session.get('mapping', {}),
        student_count_warning=report_metrics.get('student_count_warning', '')
    )

@app.route('/results')
def results():
    stats, total_students, error = calculate_clo_results()
    if error:
        flash(error)
        return redirect(url_for('index'))

    return render_template('report_results.html',
                           stats=stats,
                           total_students=total_students,
                           format_question_label=format_question_label,
                           student_count_warning=(session.get('report_metrics') or {}).get('student_count_warning', ''))

@app.route('/export-results/csv')
def export_results_csv():
    try:
        stats, total_students, error = calculate_clo_results()
    except Exception as e:
        flash(f"Error exporting CSV: {e}")
        return redirect(url_for('index'))

    if error:
        flash(error)
        return redirect(url_for('index'))

    course_info = get_course_report_info()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["CLO Attainment Report"])
    writer.writerow(["Course Name", course_info['course_name']])
    writer.writerow(["Course ID", course_info['course_id'] or "N/A"])
    writer.writerow(["Report Date", datetime.now().strftime("%Y-%m-%d")])
    writer.writerow(["Total Students Evaluated", total_students])
    writer.writerow([])
    writer.writerow(["CLO", "Mapped Questions", "Max Possible Score", "Target Score", "Target %", "Students Achieved", "Achievement %"])
    for clo, data in stats.items():
        writer.writerow([
            clo,
            ", ".join(format_question_label(question) for question in data['questions']),
            f"{data['total_possible_score']:.2f}",
            f"{data['target_score']:.2f}",
            f"{data['target_pct']:.2f}",
            data['students_achieved'],
            f"{data['achievement_percentage']:.2f}"
        ])

    response = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = 'attachment; filename="clo_achievement_report.csv"'
    return response

@app.route('/export-results/pdf')
def export_results_pdf():
    try:
        stats, total_students, error = calculate_clo_results()
    except Exception as e:
        flash(f"Error exporting PDF: {e}")
        return redirect(url_for('index'))

    if error:
        flash(error)
        return redirect(url_for('index'))

    pdf_bytes = build_results_pdf(stats, total_students, get_course_report_info())
    response = Response(pdf_bytes, mimetype="application/pdf")
    response.headers["Content-Disposition"] = 'attachment; filename="clo_achievement_report.pdf"'
    return response

if __name__ == '__main__':
    print("Starting CLO Attainment Report Generator on http://127.0.0.1:8092")
    app.run(port=8092, debug=True)
