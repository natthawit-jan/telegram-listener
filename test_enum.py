
from __future__ import annotations
import json
from enum import Enum
from typing_extensions import Self


class AccountType(str, Enum):
    """
    The type of account
    """

    """
    allowed enum values
    """
    CHECKING = 'checking'
    SAVINGS = 'savings'
    CASH = 'cash'
    CREDITCARD = 'creditCard'
    LINEOFCREDIT = 'lineOfCredit'
    OTHERASSET = 'otherAsset'
    OTHERLIABILITY = 'otherLiability'
    MORTGAGE = 'mortgage'
    AUTOLOAN = 'autoLoan'
    STUDENTLOAN = 'studentLoan'
    PERSONALLOAN = 'personalLoan'
    MEDICALDEBT = 'medicalDebt'
    OTHERDEBT = 'otherDebt'

    @classmethod
    def from_json(cls, json_str: str) -> Self:
        """Create an instance of AccountType from a JSON string"""
        return cls(json.loads(json_str))

class Status(Enum):
    PENDING = "pending"
    DONE = "done"