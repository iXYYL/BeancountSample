"""CSV importer.
"""
__copyright__ = "Copyright (C) 2018 Dongchao Li"
__license__ = "GNU GPLv2"

import csv
import datetime
import enum
import io
import collections
import re
from os import path
from typing import Union, Dict, Callable, Optional

import dateutil.parser
from beancount.core.number import D
from beancount.core.number import ZERO
from beancount.core.amount import Amount
from beancount.utils.date_utils import parse_date_liberally
from beancount.core import data
from beancount.core import flags
from beancount.ingest import importer
from beancount.ingest.importers import regexp


# The set of interpretable columns.
class Col(enum.Enum):
    # The settlement date, the date we should create the posting at.
    DATE = '[DATE]'

    # The date at which the transaction took place.
    TXN_DATE = '[TXN_DATE]'

    # The time at which the transaction took place.
    # Beancount does not support time field -- just add it to metadata.
    TXN_TIME = '[TXN_TIME]'

    # The payee field.
    PAYEE = '[PAYEE]'

    # The narration fields. Use multiple fields to combine them together.
    NARRATION = NARRATION1 = '[NARRATION1]'
    NARRATION2 = '[NARRATION2]'
    REMARK = '[REMARK]'

    # The amount being posted.
    AMOUNT = '[AMOUNT]'

    # Debits and credits being posted in separate, dedicated columns.
    AMOUNT_DEBIT = '[DEBIT]'
    AMOUNT_CREDIT = '[CREDIT]'

    # The balance amount, after the row has posted.
    BALANCE = '[BALANCE]'

    # A field to use as a tag name.
    TAG = '[TAG]'

    # A column which says DEBIT or CREDIT (generally ignored).
    DRCR = '[DRCR]'

    # Last 4 digits of the card.
    LAST4 = '[LAST4]'

    # An account name.
    ACCOUNT = '[ACCOUNT]'

    # Transaction status
    STATUS = '[STATUS]'

# The set of status which says DEBIT or CREDIT
class Debit_or_credit(enum.Enum):
    DEBIT = '[DEBIT]'

    CREDIT = '[CREDIT]'

    UNCERTAINTY = '[UNCERTAINTY]'


def get_amounts(iconfig, row, DRCR_status, allow_zero_amounts=False):
    """Get the amount columns of a row.

    Args:
      iconfig: A dict of Col to row index.
      row: A row array containing the values of the given row.
      allow_zero_amounts: Is a transaction with amount D('0.00') okay? If not,
        return (None, None).
    Returns:
      A pair of (debit-amount, credit-amount), both of which are either an
      instance of Decimal or None, or not available.
    """
    debit, credit = None, None
    if Col.AMOUNT in iconfig:
        amount = row[iconfig[Col.AMOUNT]]
        # Distinguish debit or credit
        if DRCR_status == Debit_or_credit.CREDIT:
            credit = amount
        else:
            debit = amount
    else:
        debit, credit = [row[iconfig[col]] if col in iconfig else None
                         for col in [Col.AMOUNT_DEBIT, Col.AMOUNT_CREDIT]]

    # If zero amounts aren't allowed, return null value.
    is_zero_amount = ((credit is not None and D(credit) == ZERO) and
                      (debit is not None and D(debit) == ZERO))
    if not allow_zero_amounts and is_zero_amount:
        return (None, None)

    return (-D(debit) if debit else None,
            D(credit) if credit else None)

def get_debit_or_credit_status(iconfig, row, DRCR_dict):
    """Get the status which says DEBIT or CREDIT of a row.
    """

    try:
        if Col.AMOUNT in iconfig:
            DRCR = DRCR_dict[row[iconfig[Col.DRCR]]]
            return DRCR
        else:
            if Col.AMOUNT_CREDIT in iconfig and row[iconfig[Col.AMOUNT_CREDIT]]:
                return Debit_or_credit.CREDIT
            elif Col.AMOUNT_DEBIT in iconfig and row[iconfig[Col.AMOUNT_DEBIT]]:
                return Debit_or_credit.DEBIT
            else:
                return Debit_or_credit.UNCERTAINTY
    except KeyError:
        return Debit_or_credit.UNCERTAINTY




class Importer(regexp.RegexpImporterMixin, importer.ImporterProtocol):
    """Importer for CSV files."""

    def __init__(self, config, default_account, currency, regexps,
                 skip_lines: int=0,
                 last4_map: Optional[Dict]=None,
                 categorizer: Optional[Callable]=None,
                 institution: Optional[str]=None,
                 debug: bool=False,
                 csv_dialect: Union[str, csv.Dialect] ='excel',
                 dateutil_kwds: Optional[Dict]=None,
                 narration_sep: str='; ',
                 close_flag: str='',
                 DRCR_dict: Optional[Dict]=None,
                 assets_account: Optional[Dict]=None,
                 debit_account: Optional[Dict]=None,
                 credit_account: Optional[Dict]=None):
        """Constructor.

        Args:
          config: A dict of Col enum types to the names or indexes of the columns.
          default_account: An account string, the default account to post this to.
          currency: A currency string, the currenty of this account.
          regexps: A list of regular expression strings.
          skip_lines: Skip first x (garbage) lines of file.
          last4_map: A dict that maps last 4 digits of the card to a friendly string.
          categorizer: A callable that attaches the other posting (usually expenses)
            to a transaction with only single posting.
          institution: An optional name of an institution to rename the files to.
          debug: Whether or not to print debug information
          dateutil_kwds: An optional dict defining the dateutil parser kwargs.
          csv_dialect: A `csv` dialect given either as string or as instance or
            subclass of `csv.Dialect`.
          close_flag: A string show the garbage transaction from the STATUS column.
          DRCR_dict: An optional dict of Debit_or_credit.DEBIT or Debit_or_credit.CREDIT
            to user-defined debit or credit string occurs in the DRCR column. If DRCR
            column is revealed and DRCR_dict is None, the status of trasaction will be
            uncertain
          assets_account: An optional dict of user-defined
        """
        if isinstance(regexps, str):
            regexps = [regexps]
        assert isinstance(regexps, list)
        regexp.RegexpImporterMixin.__init__(self, regexps)

        assert isinstance(config, dict)
        self.config = config

        self.default_account = default_account
        self.currency = currency
        assert isinstance(skip_lines, int)
        self.skip_lines = skip_lines
        self.last4_map = last4_map or {}
        self.debug = debug
        self.dateutil_kwds = dateutil_kwds
        self.csv_dialect = csv_dialect
        self.narration_sep = narration_sep
        self.close_flag = close_flag

        # Reverse the key and value of the DRCR_dict.
        self.DRCR_dict = dict(zip(DRCR_dict.values(), DRCR_dict.keys())) if isinstance(DRCR_dict, dict) else {}
        self.assets_account = assets_account if isinstance(assets_account, dict) else {}
        self.debit_account = debit_account if isinstance(debit_account, dict) else {}
        self.credit_account = credit_account if isinstance(credit_account, dict) else {}
        if Debit_or_credit.DEBIT not in self.debit_account:
            self.debit_account[Debit_or_credit.DEBIT] = self.default_account
        if Debit_or_credit.CREDIT not in self.credit_account:
            self.credit_account[Debit_or_credit.CREDIT] = self.default_account

        # FIXME: This probably belongs to a mixin, not here.
        self.institution = institution
        self.categorizer = categorizer

    def name(self):
        return '{}: "{}"'.format(super().name(), self.file_account(None))

    def identify(self, file):
        if file.mimetype() != 'text/csv':
            return False
        return super().identify(file)

    def file_account(self, _):
        return self.default_account

    def file_name(self, file):
        filename = path.splitext(path.basename(file.name))[0]
        if self.institution:
            filename = '{}.{}'.format(self.institution, filename)
        return '{}.csv'.format(filename)

    def file_date(self, file):
        "Get the maximum date from the file."
        iconfig, has_header = normalize_config(self.config, file.head(-1))
        if Col.DATE in iconfig:
            reader = iter(csv.reader(open(file.name)))
            for _ in range(self.skip_lines):
                next(reader)
            if has_header:
                next(reader)
            max_date = None
            for row in reader:
                if not row:
                    continue
                if row[0].startswith('#'):
                    continue
                date_str = row[iconfig[Col.DATE]]
                date = parse_date_liberally(date_str, self.dateutil_kwds)
                if max_date is None or date > max_date:
                    max_date = date
            return max_date

    def extract(self, file):
        entries = []

        # Normalize the configuration to fetch by index.
        iconfig, has_header = normalize_config(self.config, file.head(-1))

        reader = iter(csv.reader(open(file.name), dialect=self.csv_dialect))

        # Skip garbage lines
        for _ in range(self.skip_lines):
            next(reader)

        # Skip header, if one was detected.
        if has_header:
            next(reader)

        def get(row, ftype):
            try:
                return row[iconfig[ftype]] if ftype in iconfig else None
            except IndexError:  # FIXME: this should not happen
                return None

        # Parse all the transactions.
        first_row = last_row = None
        for index, row in enumerate(reader, 1):
            if not row:
                continue
            if row[0].startswith('#'):
                continue

            # If debugging, print out the rows.
            if self.debug:
                print(row)

            if first_row is None:
                first_row = row
            last_row = row

            # Extract the data we need from the row, based on the configuration.
            status = get(row, Col.STATUS)
            # When the status is CLOSED, the transaction where money had not been paid should be ignored.
            if isinstance(status,str) and status == self.close_flag:
                continue

            # Distinguish debit or credit
            DRCR_status = get_debit_or_credit_status(iconfig, row, self.DRCR_dict)


            date = get(row, Col.DATE)
            txn_date = get(row, Col.TXN_DATE)
            txn_time = get(row, Col.TXN_TIME)

            payee = get(row, Col.PAYEE)
            if payee:
                payee = payee.strip()

            fields = filter(None, [get(row, field)
                                   for field in (Col.NARRATION1,
                                                 Col.NARRATION2)])
            narration = self.narration_sep.join(field.strip() for field in fields)

            remark = get(row, Col.REMARK)

            tag = get(row, Col.TAG)
            tags = {tag} if tag is not None else data.EMPTY_SET

            last4 = get(row, Col.LAST4)

            balance = get(row, Col.BALANCE)

            # Create a transaction
            meta = data.new_metadata(file.name, index)
            if txn_date is not None:
                meta['date'] = parse_date_liberally(txn_date,
                                                    self.dateutil_kwds)
            if txn_time is not None:
                meta['time'] = str(dateutil.parser.parse(txn_time).time())
            if balance is not None:
                meta['balance'] = D(balance)
            if last4:
                last4_friendly = self.last4_map.get(last4.strip())
                meta['card'] = last4_friendly if last4_friendly else last4
            date = parse_date_liberally(date, self.dateutil_kwds)
            #flag = flags.FLAG_WARNING if DRCR_status == Debit_or_credit.UNCERTAINTY else self.FLAG
            txn = data.Transaction(meta, date, self.FLAG, payee, "{}({})".format(narration,remark),
                                   tags, data.EMPTY_SET, [])

            # Attach one posting to the transaction
            amount_debit, amount_credit = get_amounts(iconfig, row, DRCR_status)

            # Skip empty transactions
            if amount_debit is None and amount_credit is None:
                continue

            for amount in [amount_debit, amount_credit]:
                if amount is None:
                    continue
                units = Amount(amount, self.currency)

                # Uncertain transaction
                if DRCR_status == Debit_or_credit.UNCERTAINTY:
                    if remark and len(remark.split("-")) == 2:
                        remarks = remark.split("-")
                        primary_account = self.assets_account[remarks[1]]
                        secondary_account = self.assets_account[remarks[0]]
                        txn.postings.append(
                            data.Posting(primary_account, -units, None, None, None, None))
                        txn.postings.append(
                            data.Posting(secondary_account, units, None, None, None, None))
                    else:
                        txn.postings.append(
                            data.Posting(self.default_account, units, None, None, None, None))
                        

                # Debit or Credit transaction
                else:
                    # Primary posting
                    primary_account = self.default_account
                    for key in self.assets_account.keys():
                        if re.search(key, remark):
                            primary_account = self.assets_account[key]
                            break
                    txn.postings.append(
                        data.Posting(primary_account, units, None, None, None, None))
                    # Secondary posting
                    payee_narration = payee + narration
                    _account = self.credit_account
                    if DRCR_status == Debit_or_credit.DEBIT:
                        _account = self.debit_account
                    secondary_account = _account[DRCR_status]
                    for key in _account.keys():
                        if key in Debit_or_credit:
                            continue
                        if re.search(key, payee_narration):
                            secondary_account = _account[key]
                            break
                    txn.postings.append(
                        data.Posting(secondary_account, -units, None, None, None, None))

            # Attach the other posting(s) to the transaction.
            if isinstance(self.categorizer, collections.Callable):
                txn = self.categorizer(txn)

            # Add the transaction to the output list
            entries.append(txn)

        # Figure out if the file is in ascending or descending order.
        first_date = parse_date_liberally(get(first_row, Col.DATE),
                                          self.dateutil_kwds)
        last_date = parse_date_liberally(get(last_row, Col.DATE),
                                         self.dateutil_kwds)
        is_ascending = first_date < last_date

        # Reverse the list if the file is in descending order
        if not is_ascending:
            entries = list(reversed(entries))

        # Add a balance entry if possible
        if Col.BALANCE in iconfig and entries:
            entry = entries[-1]
            date = entry.date + datetime.timedelta(days=1)
            balance = entry.meta.get('balance', None)
            if balance:
                meta = data.new_metadata(file.name, index)
                entries.append(
                    data.Balance(meta, date,
                                 self.default_account, Amount(balance, self.currency),
                                 None, None))

        # Remove the 'balance' metadta.
        for entry in entries:
            entry.meta.pop('balance', None)

        return entries


def normalize_config(config, head):
    """Using the header line, convert the configuration field name lookups to int indexes.

    Args:
      config: A dict of Col types to string or indexes.
      head: A string, some decent number of bytes of the head of the file.
    Returns:
      A pair of
        A dict of Col types to integer indexes of the fields, and
        a boolean, true if the file has a header.
    Raises:
      ValueError: If there is no header and the configuration does not consist
        entirely of integer indexes.
    """
    has_header = csv.Sniffer().has_header(head)
    if has_header:
        header = next(csv.reader(io.StringIO(head)))
        field_map = {field_name.strip(): index
                     for index, field_name in enumerate(header)}
        index_config = {}
        for field_type, field in config.items():
            if isinstance(field, str):
                field = field_map[field]
            index_config[field_type] = field
    else:
        if any(not isinstance(field, int)
               for field_type, field in config.items()):
            raise ValueError("CSV config without header has non-index fields: "
                             "{}".format(config))
        index_config = config
    return index_config, has_header
