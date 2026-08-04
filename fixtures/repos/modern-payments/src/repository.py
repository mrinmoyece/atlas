from dataclasses import dataclass

@dataclass(frozen=True)
class Invoice:
    id: int
    amount_cents: int

class InvoiceRepository:
    def __init__(self, conn):
        self._conn = conn

    def get(self, invoice_id: int) -> Invoice | None:
        row = self._conn.execute(
            "SELECT id, amount_cents FROM invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        return Invoice(*row) if row else None
