"""Currency metadata; all stored amounts remain integer hundredths, no FX."""
import re
from decimal import Decimal

CURRENCIES = {'USD': ('$', 2, 1), 'RUB': ('₽', 2, 100), 'KRW': ('₩', 0, 100)}
MAX_AMOUNT = 100_000_000_000_000  # safely below JavaScript's integer limit


def valid_amount(value, currency):
    return (currency in CURRENCIES and type(value) is int
            and CURRENCIES[currency][2] <= value <= MAX_AMOUNT
            and (currency != 'KRW' or value % 100 == 0))


def parse_money(value, currency='USD'):
    if currency not in CURRENCIES or not isinstance(value, str) or len(value) > 18:
        return None
    raw = value.strip().replace(',', '.')
    pattern = r'[0-9]+(?:\.[0-9]{1,2})?' if currency != 'KRW' else r'[0-9]+'
    if not re.fullmatch(pattern, raw):
        return None
    amount = int(Decimal(raw) * 100)
    return amount if valid_amount(amount, currency) else None


def format_money(cents, currency='USD'):
    symbol, places, _ = CURRENCIES.get(currency, CURRENCIES['USD'])
    value = Decimal(int(cents)) / 100
    sign = '-' if value < 0 else ''
    amount = f'{abs(value):,.{places}f}'
    return f'{sign}${amount}' if currency == 'USD' else f'{sign}{amount} {symbol}'
