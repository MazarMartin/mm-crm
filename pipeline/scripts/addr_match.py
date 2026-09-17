"""addr_match.py — decide whether two address strings are the same property.

Shared by the fill_* scripts, which copy details (property type, baths, car,
land size, last sold price) from Domain's scrape onto listings from other
sources. They used to call two addresses the same when they shared ANY word
of 3+ letters — and those words included the suburb ("mosman") and street
types ("street", "road"). So every Mosman listing matched the first Mosman
listing in Domain's data and inherited its details. In Sep 2026 that made all
251 Mosman Proping listings "Apartment", including 5- and 6-bedroom houses,
which in turn hid them from any client looking for a house.

Now two addresses match only when the street number (including any unit),
street name and street type all agree, and the suburbs agree when both are
known. Formatting differences are tolerated:
    "3/14 Rawson Street, Mosman"  ==  "3/14 Rawson St, Mosman NSW 2088"
    "17 Holdsworth Street Neutral Bay NSW" (suburb field "Neutral Bay")
                                  ==  "17 Holdsworth St, Neutral Bay"
but "14 Rawson Street" != "3/14 Rawson Street" (a unit is not the building).
"""

import re

_TYPES = {
    'street': 'st', 'st': 'st', 'road': 'rd', 'rd': 'rd',
    'avenue': 'ave', 'ave': 'ave', 'av': 'ave',
    'drive': 'dr', 'dr': 'dr', 'place': 'pl', 'pl': 'pl',
    'court': 'ct', 'ct': 'ct', 'crescent': 'cres', 'cres': 'cres', 'cr': 'cres',
    'parade': 'pde', 'pde': 'pde', 'terrace': 'tce', 'tce': 'tce',
    'lane': 'ln', 'ln': 'ln', 'boulevard': 'blvd', 'blvd': 'blvd',
    'highway': 'hwy', 'hwy': 'hwy', 'close': 'cl', 'cl': 'cl',
    'circuit': 'cct', 'cct': 'cct', 'grove': 'gr', 'gr': 'gr',
    'esplanade': 'esp', 'esp': 'esp', 'square': 'sq', 'sq': 'sq',
}


def _clean_suburb(s):
    s = re.sub(r'[^a-z ]', ' ', (s or '').lower())
    s = re.sub(r'\bnsw\b', ' ', s)
    return ' '.join(s.split())


def address_key(address, suburb=''):
    """(street_key, suburb_key) for an address; street_key '' if unusable."""
    a = (address or '').lower()
    a = re.sub(r'\b\d{4}\s*$', ' ', a.strip())          # trailing postcode
    a = re.sub(r'\bnsw\b', ' ', a)
    parts = a.split(',')
    street = parts[0]
    sub = _clean_suburb(suburb or (parts[1] if len(parts) > 1 else ''))
    street = re.sub(r'[^a-z0-9/ ]', ' ', street)
    street = ' '.join(street.split())
    if sub and street.endswith(' ' + sub):                # "… Street Neutral Bay"
        street = street[: -len(sub)].strip()
    if not re.search(r'\d', street):                      # no house number: can't identify a property
        return '', sub.replace(' ', '')
    words = [_TYPES.get(w, w) for w in street.split()]
    return ''.join(words), sub.replace(' ', '')


def same_property(addr_a, suburb_a, addr_b, suburb_b):
    sa, suba = address_key(addr_a, suburb_a)
    sb, subb = address_key(addr_b, suburb_b)
    if not sa or sa != sb:
        return False
    return not suba or not subb or suba == subb


class AddressIndex:
    """Fast lookup of the records at the same property as a given address."""

    def __init__(self, records, addr_field='address', suburb_field='suburb'):
        self._by_street = {}
        for r in records or []:
            sk, sub = address_key(r.get(addr_field, ''), r.get(suburb_field, ''))
            if sk:
                self._by_street.setdefault(sk, []).append((sub, r))

    def find(self, address, suburb=''):
        """First record at the same property, or None."""
        sk, sub = address_key(address, suburb)
        for rsub, r in self._by_street.get(sk, []) if sk else []:
            if not sub or not rsub or sub == rsub:
                return r
        return None
