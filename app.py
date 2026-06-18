from __future__ import annotations

import csv
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from charset_normalizer import from_bytes

from flask import Flask, flash, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = 'transaction-validator-secret'
app.config['UPLOAD_FOLDER'] = Path('uploads')
app.config['OUTPUT_FOLDER'] = Path('outputs')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

app.config['UPLOAD_FOLDER'].mkdir(parents=True, exist_ok=True)
app.config['OUTPUT_FOLDER'].mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {'.csv'}
PHONE_RULES = {
    'SG': {'name': 'Singapore', 'pattern': r'^\d{8}$'},
    'IN': {'name': 'India', 'pattern': r'^\d{10}$'},
    'US': {'name': 'United States', 'pattern': r'^\d{10}$'},
}
DATE_FORMATS = [
    '%Y-%m-%d',
    '%Y-%m-%d %H:%M:%S',
    '%d/%m/%Y',
    '%d/%m/%Y %H:%M:%S',
    '%m/%d/%Y',
    '%m/%d/%Y %H:%M:%S',
    '%d-%m-%Y',
    '%d-%m-%Y %H:%M:%S',
    '%Y/%m/%d',
    '%Y/%m/%d %H:%M:%S',
    '%d.%m.%Y',
    '%d.%m.%Y %H:%M:%S'
]
PAYMENT_MODES = {'CASH', 'CARD', 'UPI', 'BANK_TRANSFER', 'WALLET', 'CHEQUE'}
REQUIRED_COLUMNS = [
    'order_id',
    'customer_name',
    'phone',
    'country_code',
    'order_date',
    'product_id',
    'product_name',
    'quantity',
    'price',
    'payment_mode'
]


def allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def detect_encoding(file_bytes: bytes) -> str:
    result = from_bytes(file_bytes)
    best = result.best()
    if best and best.encoding:
        return best.encoding
    return 'utf-8-sig'


def read_csv_rows(upload_path: Path):
    raw_bytes = upload_path.read_bytes()
    encoding = detect_encoding(raw_bytes)
    text = raw_bytes.decode(encoding, errors='replace')
    return list(csv.DictReader(text.splitlines()))


def clean_text(value):
    if value is None:
        return ''
    return str(value).strip()


def is_valid_number(value: str) -> bool:
    try:
        return float(value) >= 0
    except (TypeError, ValueError):
        return False


def validate_date(value: str) -> bool:
    if not value:
        return False
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue
    return False


def validate_phone(country_code: str, phone: str) -> bool:
    code = country_code.upper()
    rule = PHONE_RULES.get(code)
    if not rule:
        return False
    return bool(re.fullmatch(rule['pattern'], phone))


def validate_row(row: Dict[str, str]) -> Tuple[bool, List[str]]:
    errors = []
    order_id = clean_text(row.get('order_id'))
    customer_name = clean_text(row.get('customer_name'))
    phone = clean_text(row.get('phone'))
    country_code = clean_text(row.get('country_code')).upper()
    order_date = clean_text(row.get('order_date'))
    product_id = clean_text(row.get('product_id'))
    product_name = clean_text(row.get('product_name'))
    quantity = clean_text(row.get('quantity'))
    price = clean_text(row.get('price'))
    payment_mode = clean_text(row.get('payment_mode')).upper()

    if not order_id:
        errors.append('order_id is required')
    if not customer_name:
        errors.append('customer_name is required')
    if not phone:
        errors.append('phone is required')
    if not country_code:
        errors.append('country_code is required')
    if not order_date:
        errors.append('order_date is required')
    if not product_id:
        errors.append('product_id is required')
    if not product_name:
        errors.append('product_name is required')
    if not quantity:
        errors.append('quantity is required')
    if not price:
        errors.append('price is required')
    if not payment_mode:
        errors.append('payment_mode is required')

    if phone and country_code and country_code in PHONE_RULES and not validate_phone(country_code, phone):
        errors.append(f'phone format is invalid for {PHONE_RULES[country_code]["name"]}')
    elif phone and country_code and country_code not in PHONE_RULES:
        errors.append('unsupported country_code')

    if order_date and not validate_date(order_date):
        errors.append('order_date must match one of the supported formats')

    if quantity and not re.fullmatch(r'\d+', quantity):
        errors.append('quantity must be a whole number')
    elif quantity and int(quantity) <= 0:
        errors.append('quantity must be greater than zero')

    if price and not re.fullmatch(r'\d+(?:\.\d+)?', price):
        errors.append('price must be a numeric value')
    elif price and float(price) < 0:
        errors.append('price must not be negative')

    if payment_mode and payment_mode not in PAYMENT_MODES:
        errors.append('payment_mode is not in the accepted set')

    return (len(errors) == 0), errors


def make_cleaned_row(row: Dict[str, str]) -> Dict[str, str]:
    return {
        'order_id': clean_text(row.get('order_id')),
        'customer_name': clean_text(row.get('customer_name')),
        'phone': clean_text(row.get('phone')),
        'country_code': clean_text(row.get('country_code')).upper(),
        'order_date': clean_text(row.get('order_date')),
        'product_id': clean_text(row.get('product_id')),
        'product_name': clean_text(row.get('product_name')),
        'quantity': str(int(clean_text(row.get('quantity')))),
        'price': clean_text(row.get('price')),
        'payment_mode': clean_text(row.get('payment_mode')).upper(),
    }


def write_csv(path: Path, rows: List[Dict[str, str]]):
    with path.open('w', encoding='utf-8', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=REQUIRED_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def split_rows(rows: List[Dict[str, str]], chunk_size: int) -> List[Path]:
    chunk_paths: List[Path] = []
    for index, start in enumerate(range(0, len(rows), chunk_size), start=1):
        chunk_rows = rows[start:start + chunk_size]
        chunk_path = app.config['OUTPUT_FOLDER'] / f'chunk_{index:03d}.csv'
        write_csv(chunk_path, chunk_rows)
        chunk_paths.append(chunk_path)
    return chunk_paths


def cleanup_output_files() -> None:
    for file in app.config['OUTPUT_FOLDER'].glob('chunk_*.csv'):
        file.unlink(missing_ok=True)
    for file in app.config['OUTPUT_FOLDER'].glob('transaction_chunks.zip'):
        file.unlink(missing_ok=True)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/process', methods=['POST'])
def process():
    if 'file' not in request.files:
        flash('Please choose a CSV file to upload.')
        return redirect(url_for('index'))

    upload_file = request.files['file']
    if upload_file.filename == '':
        flash('No file selected.')
        return redirect(url_for('index'))

    if not allowed_file(upload_file.filename):
        flash('Only CSV files are allowed.')
        return redirect(url_for('index'))

    try:
        chunk_size = int(request.form.get('chunk_size', 200))
        if chunk_size <= 0:
            chunk_size = 200
    except ValueError:
        chunk_size = 200

    filename = secure_filename(upload_file.filename)
    base_name = Path(filename).stem
    upload_path = app.config['UPLOAD_FOLDER'] / filename
    upload_file.save(upload_path)

    try:
        reader = read_csv_rows(upload_path)
        if not reader:
            flash('The uploaded CSV is empty.')
            return redirect(url_for('index'))

        fieldnames = reader[0].keys() if reader else []
        missing = [col for col in REQUIRED_COLUMNS if col not in {name.strip().lower() for name in fieldnames}]
        if missing:
            flash(f'Missing required columns: {", ".join(missing)}')
            return redirect(url_for('index'))

        valid_rows: List[Dict[str, str]] = []
        error_rows: List[Dict[str, str]] = []

        for row_number, row in enumerate(reader, start=2):
            normalized_row = {key.strip().lower(): clean_text(value) for key, value in row.items()}
            normalized_row = {key: normalized_row.get(key, '') for key in REQUIRED_COLUMNS}
            is_valid, errors = validate_row(normalized_row)
            if is_valid:
                valid_rows.append(make_cleaned_row(normalized_row))
            else:
                error_rows.append({
                    'row_number': row_number,
                    'errors': errors,
                    'data': normalized_row,
                })

        cleanup_output_files()

        cleaned_name = f'{base_name}_cleaned.csv'
        cleaned_path = app.config['OUTPUT_FOLDER'] / cleaned_name
        write_csv(cleaned_path, valid_rows)

        chunk_paths = split_rows(valid_rows, chunk_size) if valid_rows else []

        summary = {
            'total_rows': len(reader),
            'valid_rows': len(valid_rows),
            'invalid_rows': len(error_rows),
            'chunk_size': chunk_size,
            'chunk_count': len(chunk_paths),
        }

        return render_template(
            'index.html',
            summary=summary,
            cleaned_name=cleaned_name,
            chunk_paths=chunk_paths,
            error_rows=error_rows,
            processed=True,
        )
    finally:
        if upload_path.exists():
            upload_path.unlink(missing_ok=True)


@app.route('/download/<path:filename>')
def download(filename):
    return send_from_directory(app.config['OUTPUT_FOLDER'], filename, as_attachment=True)


@app.route('/download_chunks')
def download_chunks():
    chunk_files = sorted(path.name for path in app.config['OUTPUT_FOLDER'].glob('chunk_*.csv'))
    if not chunk_files:
        flash('No chunk files are available for download.')
        return redirect(url_for('index'))

    zip_name = 'transaction_chunks.zip'
    zip_path = app.config['OUTPUT_FOLDER'] / zip_name
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file_name in chunk_files:
            archive.write(app.config['OUTPUT_FOLDER'] / file_name, file_name)

    return send_from_directory(app.config['OUTPUT_FOLDER'], zip_name, as_attachment=True)


if __name__ == '__main__':
    app.run(debug=True)
