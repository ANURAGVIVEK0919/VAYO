"""
Pydantic models for Community Outing Expense Splitter
"""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field, validator

# PART 1 — REQUEST MODELS

class CreateOutingRequest(BaseModel):
    """Request to create a new community outing."""
    community_id: str = Field(...)
    title: str = Field(...)
    created_by: str = Field(...)
    outing_date: Optional[datetime] = None
    min_karma_to_add_member: Optional[int] = Field(
        default=100,
        ge=0,
        description="Minimum karma required to add members. 0 means anyone can add."
    )


class AddMemberRequest(BaseModel):
    """Request to add a member to an outing."""
    user_id: str = Field(...)

class SplitEntry(BaseModel):
    user_id: str
    amount: int = Field(..., gt=0, description="Amount in paise")

class AddExpenseRequest(BaseModel):
    paid_by: str
    amount: int = Field(..., gt=0, description="Amount in paise")
    description: str
    split_type: str = Field(default="equal")  # "equal" or "exact"
    splits: Optional[List[SplitEntry]] = None

    @validator('split_type')
    def validate_split_type(cls, v):
        if v not in ('equal', 'exact'):
            raise ValueError("split_type must be 'equal' or 'exact'")
        return v

    @validator('splits')
    def validate_splits(cls, v, values):
        if values.get('split_type') == 'exact':
            if not v:
                raise ValueError("splits required when split_type is 'exact'")
            total = sum(s.amount for s in v)
            if total != values.get('amount'):
                raise ValueError("Sum of splits must equal total amount")
        return v

class SettleDebtRequest(BaseModel):
    """Request to settle a debt between two users."""
    from_user: str = Field(...)
    to_user: str = Field(...)
    amount: int = Field(..., gt=0, description="Amount in paise, must be greater than 0")

# PART 2 — RESPONSE MODELS

class OutingResponse(BaseModel):
    """Response model for a community outing."""
    outing_id: str
    community_id: str
    title: str
    created_by: str
    outing_date: Optional[datetime]
    status: str
    created_at: datetime
    min_karma_to_add_member: Optional[int] = 100

class ExpenseResponse(BaseModel):
    """Response model for an expense entry."""
    expense_id: str
    outing_id: str
    paid_by: str
    amount: int
    description: str
    created_at: datetime

class BalanceEntry(BaseModel):
    """Balance entry for a member in an outing."""
    user_id: str
    name: str
    total_paid: int
    total_share: int
    net_balance: int

class SimplifiedDebt(BaseModel):
    """Simplified debt settlement between two users."""
    from_user: str
    from_name: str
    to_user: str
    to_name: str
    amount: int
    upi_id: Optional[str] = None
    upi_qr_url: Optional[str] = None
    upi_deep_link: Optional[str] = None

class OutingBalanceResponse(BaseModel):
    """Response model for outing balances and simplified debts."""
    outing_id: str
    title: str
    total_expense: int
    member_count: int
    balances: List[BalanceEntry]
    simplified_debts: List[SimplifiedDebt]

# PART 3 — EXPENSE SPLIT RESPONSE MODELS

from typing import Optional, List
from datetime import datetime

class ExpenseSplitResponse(BaseModel):
    split_id: str
    expense_id: str
    user_id: str
    user_name: str
    amount: int
    status: str  # pending/accepted/rejected
    responded_at: Optional[datetime]

class PendingExpenseSplit(BaseModel):
    split_id: str
    expense_id: str
    outing_id: str
    expense_description: str
    paid_by: str
    paid_by_name: str
    amount: int
    created_at: datetime

class PendingExpenseSplitsResponse(BaseModel):
    user_id: str
    pending_count: int
    splits: List[PendingExpenseSplit]

# PART 4 — SETTLEMENT CONFIRMATION MODELS
class SettlementConfirmationResponse(BaseModel):
    confirmation_id: str
    settlement_id: str
    receiver_id: str
    status: str
    dispute_reason: Optional[str] = None
    responded_at: Optional[str] = None
    created_at: str

class DisputeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)

class PendingSettlement(BaseModel):
    settlement_id: str
    outing_id: str
    from_user: str
    from_name: str
    amount: int
    created_at: str

class PendingSettlementsResponse(BaseModel):
    user_id: str
    pending_count: int
    settlements: List[PendingSettlement]

# PART 5 — HELPER FUNCTION

def simplify_debts(balances: List[BalanceEntry]) -> List[SimplifiedDebt]:
    """
    Simplifies debts among members using a greedy algorithm.
    Returns a list of SimplifiedDebt entries.
    """
    creditors = []  # net_balance > 0
    debtors = []    # net_balance < 0
    for b in balances:
        if b.net_balance > 0:
            creditors.append({
                'user_id': b.user_id,
                'name': b.name,
                'amount': b.net_balance
            })
        elif b.net_balance < 0:
            debtors.append({
                'user_id': b.user_id,
                'name': b.name,
                'amount': -b.net_balance
            })

    simplified = []
    # Sort creditors and debtors by amount descending
    creditors.sort(key=lambda x: x['amount'], reverse=True)
    debtors.sort(key=lambda x: x['amount'], reverse=True)

    while debtors and creditors:
        debtor = debtors[0]
        creditor = creditors[0]
        settlement = min(debtor['amount'], creditor['amount'])
        simplified.append(SimplifiedDebt(
            from_user=debtor['user_id'],
            from_name=debtor['name'],
            to_user=creditor['user_id'],
            to_name=creditor['name'],
            amount=settlement
        ))
        debtor['amount'] -= settlement
        creditor['amount'] -= settlement
        if debtor['amount'] == 0:
            debtors.pop(0)
        else:
            debtors[0] = debtor
        if creditor['amount'] == 0:
            creditors.pop(0)
        else:
            creditors[0] = creditor
    return simplified
