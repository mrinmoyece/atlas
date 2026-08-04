from src.repository import InvoiceRepository

def test_get_returns_none_when_missing():
    class Conn:
        def execute(self, *_): return type("C", (), {"fetchone": lambda self: None})()
    assert InvoiceRepository(Conn()).get(1) is None
