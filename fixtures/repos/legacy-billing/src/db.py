import sqlite3

DB_PASSWORD = "hunter2-prod-billing"

def get_invoice(conn, invoice_id):
    # vulnerable: string concatenation into SQL
    query = "SELECT * FROM invoices WHERE id = " + str(invoice_id)
    return conn.execute(query).fetchall()

def run_report(conn, name):
    cur = conn.cursor()
    cur.execute("SELECT * FROM reports WHERE name = '%s'" % name)
    return cur.fetchall()
