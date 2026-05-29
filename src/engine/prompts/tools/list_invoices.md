<list_invoices>
Use this tool to list invoices visible to the caller.

Arguments:
- `uploaded_by_user_id` (optional): UUID of the user who uploaded the invoice.
- `status` (optional): one of `created`, `approved`, `rejected`, `processed`.
- `due_from` / `due_to` (optional): due-date range, YYYY-MM-DD.
- `created_from` / `created_to` (optional): invoice creation date range, YYYY-MM-DD.
- `page` (optional): page number, 30 invoices per page.

The output includes invoice ids, file names, status, due date, uploader, and a
short budgeted summary for each invoice.
</list_invoices>
