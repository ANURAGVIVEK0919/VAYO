from fastapi import APIRouter, HTTPException, status, Query
from .database import db_manager
from .splits_models import (
    CreateOutingRequest,
    AddMemberRequest,
    AddExpenseRequest,
    SettleDebtRequest,
    OutingResponse,
    ExpenseResponse,
    OutingBalanceResponse,
    BalanceEntry,
    SimplifiedDebt,
    simplify_debts,
    SettlementConfirmationResponse,
    DisputeRequest,
    PendingSettlement,
    PendingSettlementsResponse,
    ExpenseSplitResponse,
    PendingExpenseSplit,
    PendingExpenseSplitsResponse,
)
import uuid

router = APIRouter(prefix="/api/v1", tags=["Splits"])


@router.post("/outings", response_model=OutingResponse)
async def create_outing(request: CreateOutingRequest):
    """Create a new community outing and auto-add host as first member."""
    async with db_manager.pg_pool.acquire() as conn:
        community = await conn.fetchrow(
            "SELECT community_id FROM communities WHERE community_id = $1",
            request.community_id
        )
        if not community:
            raise HTTPException(status_code=404, detail="Community not found")
        user = await conn.fetchrow(
            "SELECT user_id FROM users WHERE user_id = $1",
            request.created_by
        )
        if not user:
            raise HTTPException(status_code=404, detail="Host user not found")
        member_check = await conn.fetchrow(
            "SELECT user_id FROM community_members WHERE community_id = $1 AND user_id = $2",
            request.community_id, request.created_by
        )
        if not member_check:
            raise HTTPException(status_code=403, detail="User is not a member of this community")
        outing_id = f"outing_{uuid.uuid4().hex[:10]}"
        await conn.execute(
            """
            INSERT INTO community_outings (outing_id, community_id, title, created_by, outing_date, min_karma_to_add_member)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            outing_id, request.community_id, request.title, request.created_by, 
            request.outing_date, request.min_karma_to_add_member
        )
        await conn.execute(
            "INSERT INTO outing_members (outing_id, user_id) VALUES ($1, $2)",
            outing_id, request.created_by
        )
        outing = await conn.fetchrow(
            "SELECT * FROM community_outings WHERE outing_id = $1",
            outing_id
        )
        return OutingResponse(**dict(outing))


@router.post("/outings/{outing_id}/members")
async def add_member(outing_id: str, request: AddMemberRequest, requesting_user: str):
    """Add a member to an outing."""
    async with db_manager.pg_pool.acquire() as conn:
        outing = await conn.fetchrow(
            "SELECT outing_id FROM community_outings WHERE outing_id = $1",
            outing_id
        )
        if not outing:
            raise HTTPException(status_code=404, detail="Outing not found")
        creator_row = await conn.fetchrow(
            "SELECT created_by, min_karma_to_add_member FROM community_outings WHERE outing_id = $1",
            outing_id
        )
        creator_id = creator_row['created_by']
        min_karma = creator_row['min_karma_to_add_member']

        if requesting_user != creator_id:
            # Not creator — check karma
            karma_row = await conn.fetchrow(
                    """
                    SELECT COALESCE(SUM(point_delta), 0) as total_points 
                    FROM karma_ledger 
                    WHERE user_id = $1
                    """,
                    requesting_user
                )
            user_karma = karma_row['total_points'] if karma_row else 0

            if user_karma < min_karma:
                raise HTTPException(
                    status_code=403,
                    detail=f"You need at least {min_karma} karma points to add members. You have {user_karma}."
                )
        user = await conn.fetchrow(
            "SELECT user_id FROM users WHERE user_id = $1",
            request.user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        member = await conn.fetchrow(
            "SELECT id FROM outing_members WHERE outing_id = $1 AND user_id = $2",
            outing_id, request.user_id
        )
        if member:
            raise HTTPException(status_code=409, detail="User already a member")
        await conn.execute(
            "INSERT INTO outing_members (outing_id, user_id) VALUES ($1, $2)",
            outing_id, request.user_id
        )
        return {
            "outing_id": outing_id,
            "user_id": request.user_id,
            "message": "Member added successfully"
        }


@router.post("/outings/{outing_id}/expenses", response_model=ExpenseResponse)
async def add_expense(outing_id: str, request: AddExpenseRequest):
    """Add an expense to an outing."""
    async with db_manager.pg_pool.acquire() as conn:
        outing = await conn.fetchrow(
            "SELECT outing_id, status FROM community_outings WHERE outing_id = $1",
            outing_id
        )
        if not outing:
            raise HTTPException(status_code=404, detail="Outing not found")
        if outing['status'] != 'active':
            raise HTTPException(status_code=400, detail="Outing is already settled")
        member = await conn.fetchrow(
            "SELECT id FROM outing_members WHERE outing_id = $1 AND user_id = $2",
            outing_id, request.paid_by
        )
        if not member:
            raise HTTPException(status_code=403, detail="User is not a member of this outing")
        expense_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO outing_expenses (expense_id, outing_id, paid_by, amount, description, split_type)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            expense_id, outing_id, request.paid_by, request.amount, request.description, request.split_type
        )

        # Equal split: insert for all members
        # paid_by = accepted (unhone khud pay kiya)
        # baaki sab = pending (unhe confirm karna hai)
        if request.split_type == "equal":
            members = await conn.fetch(
                "SELECT user_id FROM outing_members WHERE outing_id = $1",
                outing_id
            )
            equal_share = request.amount // len(members)
            for m in members:
                member_status = 'accepted' if m['user_id'] == request.paid_by else 'pending'
                await conn.execute(
                    """
                    INSERT INTO outing_expense_splits (expense_id, user_id, amount, status)
                    VALUES ($1, $2, $3, $4)
                    """,
                    expense_id, m['user_id'], equal_share, member_status
                )

        # Exact split: insert from request.splits
        # paid_by = accepted, baaki = pending
        elif request.split_type == "exact" and request.splits:
            for split in request.splits:
                member_status = 'accepted' if split.user_id == request.paid_by else 'pending'
                await conn.execute(
                    """
                    INSERT INTO outing_expense_splits (expense_id, user_id, amount, status)
                    VALUES ($1, $2, $3, $4)
                    """,
                    expense_id, split.user_id, split.amount, member_status
                )

        expense = await conn.fetchrow(
            "SELECT * FROM outing_expenses WHERE expense_id = $1",
            expense_id
        )
        return ExpenseResponse(**{k: str(v) if hasattr(v, "hex") else v for k, v in dict(expense).items()})


@router.post("/outings/{outing_id}/settle")
async def settle_debt(outing_id: str, request: SettleDebtRequest, settler: str):
    """Record a settlement between two members."""
    async with db_manager.pg_pool.acquire() as conn:
        outing = await conn.fetchrow(
            "SELECT outing_id, status FROM community_outings WHERE outing_id = $1",
            outing_id
        )
        if not outing:
            raise HTTPException(status_code=404, detail="Outing not found")
        if outing['status'] == 'settled':
            raise HTTPException(
                status_code=400,
                detail="This outing is already settled. No more settlements can be recorded."
            )
        from_member = await conn.fetchrow(
            "SELECT id FROM outing_members WHERE outing_id = $1 AND user_id = $2",
            outing_id, request.from_user
        )
        if not from_member:
            raise HTTPException(status_code=403, detail="from_user is not a member of this outing")
        to_member = await conn.fetchrow(
            "SELECT id FROM outing_members WHERE outing_id = $1 AND user_id = $2",
            outing_id, request.to_user
        )
        if not to_member:
            raise HTTPException(status_code=403, detail="to_user is not a member of this outing")
        if request.from_user == request.to_user:
            raise HTTPException(status_code=400, detail="Cannot settle with yourself")
        if settler != request.from_user:
            raise HTTPException(status_code=403, detail="You can only settle your own debts")
        settlement_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO outing_settlements (settlement_id, outing_id, from_user, to_user, amount)
            VALUES ($1, $2, $3, $4, $5)
            """,
            settlement_id, outing_id, request.from_user, request.to_user, request.amount
        )
        await conn.execute(
            """
            INSERT INTO outing_settlement_confirmations (id, settlement_id, receiver_id, status, created_at)
            VALUES ($1, $2, $3, $4, NOW())
            """,
            str(uuid.uuid4()), settlement_id, request.to_user, 'pending'
        )
        return {
            "outing_id": outing_id,
            "from_user": request.from_user,
            "to_user": request.to_user,
            "amount": request.amount,
            "message": "Settlement recorded successfully",
            "outing_status": "active"
        }


@router.post("/settlements/{settlement_id}/confirm", response_model=SettlementConfirmationResponse)
async def confirm_settlement(settlement_id: str, confirming_user: str):
    """Confirm a settlement."""
    async with db_manager.pg_pool.acquire() as conn:
        settlement = await conn.fetchrow(
            "SELECT * FROM outing_settlements WHERE settlement_id = $1",
            settlement_id
        )
        if not settlement:
            raise HTTPException(status_code=404, detail="Settlement not found")
        confirmation = await conn.fetchrow(
            "SELECT * FROM outing_settlement_confirmations WHERE settlement_id = $1",
            settlement_id
        )
        if not confirmation:
            raise HTTPException(status_code=404, detail="Confirmation not found")
        if confirming_user != confirmation['receiver_id']:
            raise HTTPException(status_code=403, detail="Only the receiver can confirm this settlement")
        await conn.execute(
            "UPDATE outing_settlement_confirmations SET status = 'confirmed', responded_at = NOW() WHERE settlement_id = $1",
            settlement_id
        )
        outing_id = settlement['outing_id']
        members = await conn.fetch(
            """
            SELECT om.user_id, u.username
            FROM outing_members om
            JOIN users u ON u.user_id = om.user_id
            WHERE om.outing_id = $1
            """,
            outing_id
        )
        member_list = [dict(m) for m in members]
        member_count = len(member_list)
        expenses = await conn.fetch(
            "SELECT expense_id, paid_by, amount, split_type FROM outing_expenses WHERE outing_id = $1",
            outing_id
        )
        expense_list = [dict(e) for e in expenses]
        expense_splits = await conn.fetch(
            """
            SELECT expense_id, user_id, amount
            FROM outing_expense_splits
            WHERE expense_id = ANY($1::uuid[])
            AND status = 'accepted'
            """,
            [e["expense_id"] for e in expenses]
        )
        splits_list = [dict(s) for s in expense_splits]
        settlements = await conn.fetch(
            "SELECT from_user, to_user, amount FROM outing_settlements WHERE outing_id = $1",
            outing_id
        )
        settlement_list = [dict(s) for s in settlements]
        balances = []
        for m in member_list:
            uid = m['user_id']
            total_paid = sum(e['amount'] for e in expense_list if e['paid_by'] == uid)
            received = sum(s['amount'] for s in settlement_list if s['to_user'] == uid)
            paid_out = sum(s['amount'] for s in settlement_list if s['from_user'] == uid)
            total_share = 0
            for exp in expense_list:
                member_split = next(
                    (s['amount'] for s in splits_list
                     if s['user_id'] == uid and s['expense_id'] == exp['expense_id']),
                    0
                )
                total_share += member_split
            net_balance = (total_paid - total_share) - received + paid_out
            balances.append(BalanceEntry(
                user_id=uid,
                name=m['username'],
                total_paid=total_paid,
                total_share=total_share,
                net_balance=net_balance
            ))
        simplified_debts = simplify_debts(balances)
        if not simplified_debts:
            await conn.execute(
                "UPDATE community_outings SET status = 'settled' WHERE outing_id = $1",
                outing_id
            )
        updated = await conn.fetchrow(
            "SELECT * FROM outing_settlement_confirmations WHERE settlement_id = $1",
            settlement_id
        )
        return SettlementConfirmationResponse(
            confirmation_id=str(updated['id']),
            settlement_id=str(updated['settlement_id']),
            receiver_id=str(updated['receiver_id']),
            status=updated['status'],
            dispute_reason=updated['dispute_reason'],
            responded_at=str(updated['responded_at']) if updated['responded_at'] else None,
            created_at=str(updated['created_at'])
        )


@router.post("/settlements/{settlement_id}/dispute", response_model=SettlementConfirmationResponse)
async def dispute_settlement(settlement_id: str, disputing_user: str, request: DisputeRequest):
    """Dispute a settlement."""
    async with db_manager.pg_pool.acquire() as conn:
        settlement = await conn.fetchrow(
            "SELECT * FROM outing_settlements WHERE settlement_id = $1",
            settlement_id
        )
        if not settlement:
            raise HTTPException(status_code=404, detail="Settlement not found")
        confirmation = await conn.fetchrow(
            "SELECT * FROM outing_settlement_confirmations WHERE settlement_id = $1",
            settlement_id
        )
        if not confirmation:
            raise HTTPException(status_code=404, detail="Confirmation not found")
        if disputing_user != confirmation['receiver_id']:
            raise HTTPException(status_code=403, detail="Only the receiver can dispute this settlement")
        await conn.execute(
            "UPDATE outing_settlement_confirmations SET status = 'disputed', dispute_reason = $2, responded_at = NOW() WHERE settlement_id = $1",
            settlement_id, request.reason
        )
        updated = await conn.fetchrow(
            "SELECT * FROM outing_settlement_confirmations WHERE settlement_id = $1",
            settlement_id
        )
        return SettlementConfirmationResponse(
            confirmation_id=str(updated['id']),
            settlement_id=str(updated['settlement_id']),
            receiver_id=str(updated['receiver_id']),
            status=updated['status'],
            dispute_reason=updated['dispute_reason'],
            responded_at=str(updated['responded_at']) if updated['responded_at'] else None,
            created_at=str(updated['created_at'])
        )


@router.get("/settlements/{settlement_id}")
async def get_settlement(settlement_id: str):
    """Get settlement details."""
    async with db_manager.pg_pool.acquire() as conn:
        settlement = await conn.fetchrow(
            "SELECT * FROM outing_settlements WHERE settlement_id = $1",
            settlement_id
        )
        if not settlement:
            raise HTTPException(status_code=404, detail="Settlement not found")
        confirmation = await conn.fetchrow(
            "SELECT * FROM outing_settlement_confirmations WHERE settlement_id = $1",
            settlement_id
        )
        return {
            "settlement": dict(settlement),
            "confirmation": dict(confirmation) if confirmation else None
        }


@router.get("/users/{user_id}/pending-settlements", response_model=PendingSettlementsResponse)
async def get_pending_settlements(user_id: str):
    """Get all pending settlements for a user."""
    async with db_manager.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.settlement_id, s.outing_id, s.from_user, u.username as from_name,
                   s.amount, s.settled_at, c.created_at
            FROM outing_settlements s
            JOIN outing_settlement_confirmations c ON c.settlement_id = s.settlement_id
            JOIN users u ON u.user_id = s.from_user
            WHERE s.to_user = $1 AND c.status = 'pending'
            """,
            user_id
        )
        settlements = [PendingSettlement(
            settlement_id=str(r['settlement_id']),
            outing_id=str(r['outing_id']),
            from_user=str(r['from_user']),
            from_name=r['from_name'],
            amount=r['amount'],
            created_at=str(r['created_at'])
        ) for r in rows]
        return PendingSettlementsResponse(
            user_id=user_id,
            pending_count=len(settlements),
            settlements=settlements
        )


@router.get("/outings/{outing_id}", response_model=OutingResponse)
async def get_outing(outing_id: str):
    """Get single outing details."""
    async with db_manager.pg_pool.acquire() as conn:
        outing = await conn.fetchrow(
            "SELECT * FROM community_outings WHERE outing_id = $1",
            outing_id
        )
        if not outing:
            raise HTTPException(status_code=404, detail="Outing not found")
        return OutingResponse(**dict(outing))


@router.get("/outings/{outing_id}/balances", response_model=OutingBalanceResponse)
async def get_balances(outing_id: str):
    """Get balances and simplified debts for an outing."""
    async with db_manager.pg_pool.acquire() as conn:
        outing = await conn.fetchrow(
            "SELECT outing_id, title FROM community_outings WHERE outing_id = $1",
            outing_id
        )
        if not outing:
            raise HTTPException(status_code=404, detail="Outing not found")
        members = await conn.fetch(
            """
            SELECT om.user_id, u.username
            FROM outing_members om
            JOIN users u ON u.user_id = om.user_id
            WHERE om.outing_id = $1
            """,
            outing_id
        )
        member_list = [dict(m) for m in members]
        member_count = len(member_list)
        expenses = await conn.fetch(
            "SELECT expense_id, paid_by, amount, split_type FROM outing_expenses WHERE outing_id = $1",
            outing_id
        )
        expense_list = [dict(e) for e in expenses]
        total_expense = sum(e['amount'] for e in expense_list)
        expense_splits = await conn.fetch(
            """
            SELECT expense_id, user_id, amount
            FROM outing_expense_splits
            WHERE expense_id = ANY($1::uuid[])
            AND status = 'accepted'
            """,
            [e["expense_id"] for e in expenses]
        )
        splits_list = [dict(s) for s in expense_splits]
        settlements = await conn.fetch(
            """
            SELECT from_user, to_user, amount
            FROM outing_settlements
            WHERE outing_id = $1
            """,
            outing_id
        )
        settlement_list = [dict(s) for s in settlements]
        balances = []
        for m in member_list:
            uid = m['user_id']
            total_paid = sum(e['amount'] for e in expense_list if e['paid_by'] == uid)
            received = sum(s['amount'] for s in settlement_list if s['to_user'] == uid)
            paid_out = sum(s['amount'] for s in settlement_list if s['from_user'] == uid)
            total_share = 0
            for exp in expense_list:
                member_split = next(
                    (s['amount'] for s in splits_list
                     if s['user_id'] == uid and s['expense_id'] == exp['expense_id']),
                    0
                )
                total_share += member_split
            net_balance = (total_paid - total_share) - received + paid_out
            balances.append(BalanceEntry(
                user_id=uid,
                name=m['username'],
                total_paid=total_paid,
                total_share=total_share,
                net_balance=net_balance
            ))
        simplified_debts = simplify_debts(balances)

        # Fetch UPI info for each creditor (to_user)
        for debt in simplified_debts:
            upi_row = await conn.fetchrow(
                "SELECT upi_id FROM users WHERE user_id = $1",
                debt.to_user
            )
            if upi_row and upi_row['upi_id']:
                debt.upi_id = upi_row['upi_id']
                debt.upi_qr_url = f"/api/v1/users/{debt.to_user}/upi/qr?amount={debt.amount}"
                debt.upi_deep_link = f"upi://pay?pa={upi_row['upi_id']}&pn={debt.to_name}&am={debt.amount/100:.2f}&cu=INR"

        return OutingBalanceResponse(
            outing_id=outing_id,
            title=outing['title'],
            total_expense=total_expense,
            member_count=member_count,
            balances=balances,
            simplified_debts=simplified_debts
        )


@router.get("/outings/{outing_id}/expenses")
async def list_outing_expenses(outing_id: str):
    """List all expenses with their split status."""
    async with db_manager.pg_pool.acquire() as conn:
        outing = await conn.fetchrow(
            "SELECT outing_id FROM community_outings WHERE outing_id = $1",
            outing_id
        )
        if not outing:
            raise HTTPException(status_code=404, detail="Outing not found")
        rows = await conn.fetch(
            """
            SELECT e.expense_id, e.outing_id, e.paid_by,
                   u.username as paid_by_name,
                   e.amount, e.description, e.split_type, e.created_at
            FROM outing_expenses e
            JOIN users u ON u.user_id = e.paid_by
            WHERE e.outing_id = $1
            ORDER BY e.created_at ASC
            """,
            outing_id
        )
        expenses = []
        for r in rows:
            expense_dict = dict(r)
            splits = await conn.fetch(
                """
                SELECT oes.user_id, u.username, oes.amount, oes.status
                FROM outing_expense_splits oes
                JOIN users u ON u.user_id = oes.user_id
                WHERE oes.expense_id = $1
                ORDER BY oes.status
                """,
                r['expense_id']
            )
            expense_dict['splits'] = [dict(s) for s in splits]
            expenses.append(expense_dict)
        total = sum(r['amount'] for r in rows)
        return {
            "outing_id": outing_id,
            "expenses": expenses,
            "total_expense": total
        }


@router.get("/communities/{community_id}/outings")
async def list_community_outings(community_id: str):
    """List all outings for a community, most recent first."""
    async with db_manager.pg_pool.acquire() as conn:
        community = await conn.fetchrow(
            "SELECT community_id FROM communities WHERE community_id = $1",
            community_id
        )
        if not community:
            raise HTTPException(status_code=404, detail="Community not found")
        rows = await conn.fetch(
            """
            SELECT * FROM community_outings
            WHERE community_id = $1
            ORDER BY created_at DESC
            """,
            community_id
        )
        return {"community_id": community_id, "outings": [dict(r) for r in rows]}


@router.post("/expenses/{expense_id}/splits/{user_id}/accept")
async def accept_expense_split(expense_id: str, user_id: str, accepting_user: str):
    """Accept an expense split."""
    async with db_manager.pg_pool.acquire() as conn:
        split = await conn.fetchrow(
            "SELECT * FROM outing_expense_splits WHERE expense_id = $1 AND user_id = $2",
            expense_id, user_id
        )
        if not split:
            raise HTTPException(status_code=404, detail="Split not found")
        if accepting_user != user_id:
            raise HTTPException(status_code=403, detail="You can only accept your own splits")
        if split['status'] != 'pending':
            raise HTTPException(status_code=400, detail="Split already responded to")
        await conn.execute(
            """
            UPDATE outing_expense_splits
            SET status = 'accepted', responded_at = NOW()
            WHERE expense_id = $1 AND user_id = $2
            """,
            expense_id, user_id
        )
        return {
            "expense_id": expense_id,
            "user_id": user_id,
            "status": "accepted",
            "message": "Expense split accepted"
        }


@router.post("/expenses/{expense_id}/splits/{user_id}/reject")
async def reject_expense_split(expense_id: str, user_id: str, rejecting_user: str):
    """Reject an expense split."""
    async with db_manager.pg_pool.acquire() as conn:
        split = await conn.fetchrow(
            "SELECT * FROM outing_expense_splits WHERE expense_id = $1 AND user_id = $2",
            expense_id, user_id
        )
        if not split:
            raise HTTPException(status_code=404, detail="Split not found")
        if rejecting_user != user_id:
            raise HTTPException(status_code=403, detail="You can only reject your own splits")
        if split['status'] != 'pending':
            raise HTTPException(status_code=400, detail="Split already responded to")
        await conn.execute(
            """
            UPDATE outing_expense_splits
            SET status = 'rejected', responded_at = NOW()
            WHERE expense_id = $1 AND user_id = $2
            """,
            expense_id, user_id
        )
        return {
            "expense_id": expense_id,
            "user_id": user_id,
            "status": "rejected",
            "message": "Expense split rejected"
        }


@router.get("/users/{user_id}/pending-expense-splits",
            response_model=PendingExpenseSplitsResponse)
async def get_pending_expense_splits(user_id: str):
    """Get all pending expense splits for a user."""
    async with db_manager.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                oes.id as split_id,
                oes.expense_id,
                oe.outing_id,
                oe.description as expense_description,
                oe.paid_by,
                u.username as paid_by_name,
                oes.amount,
                oe.created_at
            FROM outing_expense_splits oes
            JOIN outing_expenses oe ON oe.expense_id = oes.expense_id
            JOIN users u ON u.user_id = oe.paid_by
            WHERE oes.user_id = $1
            AND oes.status = 'pending'
            ORDER BY oe.created_at DESC
            """,
            user_id
        )
        splits = [PendingExpenseSplit(
            split_id=str(r['split_id']),
            expense_id=str(r['expense_id']),
            outing_id=str(r['outing_id']),
            expense_description=r['expense_description'],
            paid_by=str(r['paid_by']),
            paid_by_name=r['paid_by_name'],
            amount=r['amount'],
            created_at=r['created_at']
        ) for r in rows]
        return PendingExpenseSplitsResponse(
            user_id=user_id,
            pending_count=len(splits),
            splits=splits
        )