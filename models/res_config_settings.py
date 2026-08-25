import uuid
import psycopg2

from odoo import models, _
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    """
        This class extends the `res.config.settings` model to provide functionality for
        data elimination, dependency management, and resetting sequences in an Odoo database.
        It includes methods for clearing data across various models, handling dependencies,
        and managing database transactions.
    """
    _inherit = "res.config.settings"

    _model_dependencies = {
        "product.product": [
            "account.move.line", "sale.order.line", "purchase.order.line",
            "stock.move", "stock.move.line", "pos.order.line", "mrp.bom.line",
        ],
        "product.template": ["product.product"],
        "sale.order": ["sale.order.line"],
        "purchase.order": ["purchase.order.line"],
        "account.move": ["account.move.line"],
        "mrp.production": ["mrp.workorder", "stock.move"],
        "stock.picking": ["stock.move", "stock.move.line"],
    }
    _clearance_groups = [
        ["account.bank.statement.line", "account.payment", "account.partial.reconcile",
            "account.move.line", "account.move", "payment.transaction"],
        ["pos.payment", "pos.order.line", "pos.order", "pos.session",
            "sale.order.line", "sale.order"],
        ["purchase.order.line", "purchase.order", "purchase.requisition.line",
            "purchase.requisition"],
        ["stock.move.line", "stock.package_level", "stock.move", "stock.quant",
            "stock.picking", "stock.scrap", "stock.inventory.line", "stock.inventory",
            "stock.valuation.layer", "stock.production.lot", "procurement.group"],
        ["mrp.workorder", "mrp.production.workcenter.line", "mrp.production",
            "mrp.production.product.line", "mrp.unbuild", "mrp.bom.line", "mrp.bom"],
        ["product.product", "product.template"],
        ["mail.message", "mail.followers", "mail.activity"],
    ]

    def data_elimination_with_transaction(self, o, s=None, ignore_errors=False):
        """
        Perform data elimination with transaction management.
        """
        if s is None:
            s = []
        success = True
        savepoint_name = f"data_elimination_{str(uuid.uuid4()).replace('-', '_')}"
        try:
            self._cr.execute(f"SAVEPOINT {savepoint_name}")
            self._cr.execute("SET session_replication_role = replica;")
            for line in o:
                try:
                    if not self.env["ir.model"].search([("model", "=", line)], limit=1):
                        continue
                except KeyError:
                    if not ignore_errors:
                        success = False
                    continue
                obj_name = line
                obj = self.pool.get(obj_name)
                if not obj:
                    t_name = obj_name.replace(".", "_")
                else:
                    t_name = obj._table
                model_savepoint = f"del_model_{t_name}_{str(uuid.uuid4()).replace('-', '_')}"
                try:
                    self._cr.execute(f"SAVEPOINT {model_savepoint}")
                    deleted_count = 1
                    batch_size = 5000
                    while deleted_count > 0:
                        try:
                            sql = f"""DELETE FROM {t_name} WHERE id IN
                                (SELECT id FROM {t_name} LIMIT {batch_size})"""
                            self._cr.execute(sql)
                            deleted_count = self._cr.rowcount
                        except psycopg2.DatabaseError:
                            deleted_count = 0
                            if not ignore_errors:
                                success = False
                                raise
                    self._cr.execute(f"RELEASE SAVEPOINT {model_savepoint}")
                except psycopg2.DatabaseError:
                    self._cr.execute(f"ROLLBACK TO SAVEPOINT {model_savepoint}")
                    self._cr.execute(f"RELEASE SAVEPOINT {model_savepoint}")
                    if not ignore_errors:
                        success = False
            for line in s:
                try:
                    domain = ["|",
                        ("code", "=ilike", line + "%"),
                        ("prefix", "=ilike", line + "%"),
                    ]
                    seqs = self.env["ir.sequence"].sudo().search(domain)
                    if seqs.exists():
                        seqs.number_next = 1
                except psycopg2.DatabaseError:
                    if not ignore_errors:
                        success = False
            self._cr.execute("SET session_replication_role = DEFAULT;")
            if success:
                self._cr.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            else:
                # If there were errors but we're ignoring them, still release
                if ignore_errors:
                    self._cr.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                else:
                    self._cr.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
        except (psycopg2.DatabaseError, ValueError):
            success = False
            try:
                self._cr.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                self._cr.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            except psycopg2.DatabaseError:
                self._cr.rollback()
        return success

    def data_elimination_with_retries(self, model_list, sequences=None,
        max_retries=3, batch_size=1000):
        """
        Deletes records from specified models in batches with retry logic
        and resets sequence numbers.
        """
        if sequences is None:
            sequences = []
        self.ensure_one()
        for model_name in model_list:
            retries = 0
            success = False
            while not success and retries < max_retries:
                try:
                    model = self.env.get(model_name)
                    if not model:
                        break
                    while True:
                        records = model.sudo().search([], limit=batch_size)
                        if not records:
                            break
                        records.unlink()
                    success = True
                except psycopg2.DatabaseError:
                    self.env.cr.rollback()
                    retries += 1
            if not success:
                raise UserError(
                    _(f"Failed to clear data for model {model_name} after multiple attempts."))
        for seq_prefix in sequences:
            domain = [
                "|",
                ("code", "=ilike", seq_prefix + "%"),
                ("prefix", "=ilike", seq_prefix + "%"),
            ]
            seqs = self.env["ir.sequence"].sudo().search(domain)
            if seqs:
                seqs.number_next = 1
        return True

    def clear_all_with_dependencies(self):
        """
        Clears all data with dependencies and resets sequence numbers.
        """
        for group in self._clearance_groups:
            self.data_elimination_with_transaction(group)
        sequences_to_reset = [
            "sale", "purchase.", "stock.", "picking.", "product.product", "pos.",
            "mrp.", "hr.expense.", "quality.check", "quality.alert", "WH/",
            "procurement.group", "product.tracking.default"
        ]
        account_sequences = [
            "account.%", "BNK1/%", "CSH1/%", "INV/%",
            "EXCH/%", "MISC/%", "账单/%", "杂项/%"
        ]
        for seq in sequences_to_reset + account_sequences:
            domain = ["|", ("code", "=ilike", seq), ("prefix", "=ilike", seq)]
            seqs = self.env["ir.sequence"].sudo().search(domain)
            if seqs.exists():
                seqs.number_next = 1
        self.reset_category_location_name()
        return True

    def clear_data_safely(self, model_name):
        """
        Safely clears data for the specified model and its dependencies.
        """
        dependencies = self._find_all_dependencies(model_name)
        for dep in reversed(dependencies):
            self.data_elimination_with_transaction([dep])
        return self.data_elimination_with_transaction([model_name])

    def _find_all_dependencies(self, model_name, visited=None):
        if visited is None:
            visited = set()
        if model_name in visited:
            return []
        visited.add(model_name)
        result = [model_name]
        dependencies = self._model_dependencies.get(model_name, [])
        for dep in dependencies:
            if dep not in visited:
                result.extend(self._find_all_dependencies(dep, visited))
        return result

    def clear_sales(self):
        """
        Clears sales-related data by eliminating specified models and sequences.
        """
        to_elimination = ["sale.order.line", "sale.order"]
        seqs = ["sale"]
        return self.data_elimination_with_transaction(to_elimination, seqs)

    def clear_product(self):
        """
        Clears product-related data from the database.
        """
        # First check if account module is installed
        if self.env["ir.model"]._get("account.move.line"):
            # Check if table exists
            self._cr.execute("""SELECT EXISTS (SELECT 1
                FROM information_schema.tables
                WHERE table_name = 'account_move_line')""")
            if self._cr.fetchone()[0]:
                # Now safely check for product references
                self._cr.execute("""SELECT EXISTS (SELECT 1
                    FROM account_move_line aml
                    JOIN product_product pp ON aml.product_id = pp.id
                    LIMIT 1)""")
                has_account_data = self._cr.fetchone()[0]
                if has_account_data:
                    self._cr.execute("DELETE FROM account_move_line WHERE product_id IS NOT NULL")
                    self._cr.commit()
        # Handle dependencies
        dependencies = [
            "sale.order.line", "purchase.order.line", "stock.move",
            "stock.move.line", "pos.order.line", "mrp.bom.line"
        ]
        for dep in dependencies:
            if self.env["ir.model"]._get(dep):
                self.data_elimination_with_transaction([dep], ignore_errors=True)
        # Clear products
        to_elimination = ["product.product", "product.template"]
        seqs = ["product.product"]
        return self.data_elimination_with_transaction(to_elimination, seqs)

    def clear_product_attribute(self):
        """
        Clears product attribute data by eliminating specific models.
        """
        to_elimination = ["product.attribute.value", "product.attribute"]
        return self.data_elimination_with_transaction(to_elimination)

    def clear_pos(self):
        """
        Clears Point of Sale (POS) related data and updates account bank statement balances.
        """
        to_elimination = ["pos.payment","pos.order.line", "pos.order", "pos.session"]
        seqs = ["pos."]
        res = self.data_elimination_with_transaction(to_elimination, seqs)
        # Update bank statement balances
        statements = self.env["account.bank.statement"].sudo().search([])
        for statement in statements:
            # Calculate ending balance based on lines
            balance = statement.balance_start
            for line in statement.line_ids:
                balance += line.amount
            statement.write({
                'balance_end_real': balance,
                'balance_end': balance
            })
        return res

    def clear_purchase(self):
        """
        Clears purchase-related data by eliminating specified models and sequences.
        """
        to_elimination = [
            "purchase.order.line", "purchase.order",
            "purchase.requisition.line", "purchase.requisition"
        ]
        seqs = ["purchase."]
        return self.data_elimination_with_transaction(to_elimination, seqs)

    def clear_expense(self):
        """
        Clears expense-related data by eliminating specified models and sequences.
        """
        to_elimination = [
            "hr.expense.sheet", "hr.expense", "hr.payslip", "hr.payslip.run"
        ]
        seqs = ["hr.expense."]
        return self.data_elimination_with_transaction(to_elimination, seqs)

    def clear_mrp(self):
        """
        Clears Manufacturing Resource Planning (MRP) related data by eliminating
        specific records and resetting associated sequences.
        """
        to_elimination = [
            "mrp.workcenter.productivity", "mrp.workorder", "mrp.production.workcenter.line",
            "change.production.qty", "mrp.production", "mrp.production.product.line",
            "mrp.unbuild", "change.production.qty", "sale.forecast.indirect", "sale.forecast",
        ]
        seqs = ["mrp."]
        return self.data_elimination_with_transaction(to_elimination, seqs)

    def clear_mrp_bom(self):
        """
        Clears Manufacturing Bill of Materials (BOM) data.
        """
        to_elimination = ["mrp.bom.line", "mrp.bom"]
        return self.data_elimination_with_transaction(to_elimination)

    def clear_inventory(self):
        """
        Clears inventory-related data by eliminating records from specified models
        and resetting associated sequences.
        """
        to_elimination = [
            "stock.quant", "stock.move.line", "stock.package_level", "stock.quantity.history",
            "stock.quant.package", "stock.move", "stock.picking", "stock.scrap",
            "stock.picking.batch", "stock.inventory.line", "stock.inventory",
            "stock.valuation.layer", "stock.production.lot", "procurement.group",
        ]
        seqs = [
            "stock.", "picking.", "procurement.group",
            "product.tracking.default", "WH/",
        ]
        return self.data_elimination_with_transaction(to_elimination, seqs)

    def clear_account(self):
        """Clears account-related data from the database for the current company."""
        # Set replica role to bypass triggers
        self._cr.execute("SET session_replication_role = replica;")
        # Delete in correct dependency order
        deletion_order = [
            "account.move.line", "account.partial.reconcile", "account.payment",
            "account.bank.statement.line", "account.move", "account.analytic.line",
            "account.analytic.account", "payment.transaction"
        ]
        # Delete records in batches
        batch_size = 5000
        company_id = self.env.company.id
        # Process each model in the deletion order
        for model_name in deletion_order:
            if not self.env["ir.model"]._get(model_name):
                continue
            model_obj = self.env[model_name]
            table_name = model_obj._table
            # Check if company_id column exists
            self._cr.execute(
                """SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = %s AND column_name = 'company_id'
                )""", (table_name,))
            has_company_id = self._cr.fetchone()[0]
            # Build company clause once outside the loop
            company_clause = f"WHERE company_id = {company_id}" if has_company_id else ""
            # Delete in batches until no more records are found
            while True:
                self._cr.execute(
                    f"""DELETE FROM {table_name} WHERE id IN (
                        SELECT id FROM {table_name} {company_clause} LIMIT {batch_size}
                    )""")
                if self._cr.rowcount == 0:
                    break
        # Reset sequences
        seqs = self.env["ir.sequence"].search([
            ("company_id", "=", company_id),
            "|", ("code", "=ilike", "account.%"),
            "|", ("prefix", "=ilike", "BNK1/%"),
            "|", ("prefix", "=ilike", "CSH1/%"),
            "|", ("prefix", "=ilike", "INV/%"),
            "|", ("prefix", "=ilike", "EXCH/%"),
            "|", ("prefix", "=ilike", "MISC/%"),
            "|", ("prefix", "=ilike", "账单/%"),
            ("prefix", "=ilike", "杂项/%"),
        ])
        if seqs:
            seqs.number_next = 1
        # Reset to default role
        self._cr.execute("SET session_replication_role = DEFAULT;")
        return True

    def clear_account_chart(self):
        """
        Clears the account chart and related financial data for the current company.
        """
        company_id = self.env.company.id
        self_with_context = self.with_context(
            force_company=company_id, company_id=company_id)
        to_elimination = [
            "res.partner.bank", "account.move.line", "account.invoice",
            "account.payment", "account.bank.statement", "account.tax.account.tag",
            "account.tax", "account.account.account.tag", "wizard_multi_charts_accounts",
            "account.journal", "account.account",
        ]
        self_with_context.env.cr.rollback()
        self._cr.rollback()
        try:
            try:
                field1 = (
                    self.env["ir.model.fields"]._get("product.template", "taxes_id").id)
                field2 = (
                    self.env["ir.model.fields"]._get("product.template", "supplier_taxes_id").id)
                sql = f"""DELETE FROM ir_default WHERE (
                    field_id = {field1} OR field_id = {field2}) AND company_id={company_id}"""
                sql2 = f"""UPDATE account_journal SET bank_account_id=NULL
                    WHERE company_id={company_id};"""
                self._cr.execute(sql)
                self._cr.execute(sql2)
                self._cr.commit()
            except psycopg2.DatabaseError:
                self._cr.rollback()
            if self.env["ir.model"]._get("pos.config"):
                try:
                    self._cr.execute("SET session_replication_role = replica;")
                    self._cr.execute("UPDATE pos_config SET journal_id = NULL;")
                    self._cr.execute("SET session_replication_role = DEFAULT;")
                    self._cr.commit()
                except psycopg2.DatabaseError:
                    self._cr.rollback()
            try:
                self._cr.execute("SET session_replication_role = replica;")
                self._cr.execute("""UPDATE res_partner SET property_account_receivable_id = NULL,
                    property_account_payable_id = NULL;""")
                self._cr.execute("SET session_replication_role = DEFAULT;")
                self._cr.commit()
            except psycopg2.DatabaseError:
                self._cr.rollback()
            try:
                self._cr.execute("SET session_replication_role = replica;")
                self._cr.execute(
                    """
                    UPDATE product_category
                    SET property_account_income_categ_id = NULL,
                        property_account_expense_categ_id = NULL,
                        property_account_creditor_price_difference_categ = NULL,
                        property_stock_account_input_categ_id = NULL,
                        property_stock_account_output_categ_id = NULL,
                        property_stock_valuation_account_id = NULL;
                """)
                self._cr.execute("SET session_replication_role = DEFAULT;")
                self._cr.commit()
            except psycopg2.DatabaseError:
                self._cr.rollback()
            try:
                self._cr.execute("SET session_replication_role = replica;")
                self._cr.execute(
                    """UPDATE product_template SET property_account_income_id = NULL,
                       property_account_expense_id = NULL;""")
                self._cr.execute("SET session_replication_role = DEFAULT;")
                self._cr.commit()
            except psycopg2.DatabaseError:
                self._cr.rollback()
            try:
                self._cr.execute("SET session_replication_role = replica;")
                self._cr.execute(
                    """UPDATE stock_location SET valuation_in_account_id = NULL,
                        valuation_out_account_id = NULL;""")
                self._cr.execute("SET session_replication_role = DEFAULT;")
                self._cr.commit()
            except psycopg2.DatabaseError:
                self._cr.rollback()
            for model in to_elimination:
                try:
                    savepoint = "sp_" + model.replace(".", "_")
                    self._cr.execute(f"SAVEPOINT {savepoint}")
                    success = self.data_elimination_with_transaction(
                        [model], ignore_errors=True)
                    if success:
                        self._cr.execute(f"RELEASE SAVEPOINT {savepoint}")
                    else:
                        self._cr.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                except psycopg2.DatabaseError:
                    self._cr.rollback()
            self._cr.commit()
            return True
        except psycopg2.DatabaseError:
            self._cr.rollback()
            return False

    def clear_project(self):
        """
        Clears project-related data by eliminating records from specified models.
        """
        to_elimination = [
            "account.analytic.line", "project.task",
            "project.forecast", "project.project"
        ]
        return self.data_elimination_with_transaction(to_elimination)

    def clear_quality(self):
        """
        Clears quality-related data by eliminating specified models and their sequences.
        """
        to_elimination = ["quality.check", "quality.alert"]
        seqs = ["quality.check", "quality.alert"]
        return self.data_elimination_with_transaction(to_elimination, seqs)

    def clear_quality_setting(self):
        """
        Clears quality-related settings by eliminating specific data models.
        """
        to_elimination = [
            "quality.point", "quality.alert.stage", "quality.alert.team",
            "quality.point.test_type", "quality.reason", "quality.tag"
        ]
        return self.data_elimination_with_transaction(to_elimination)

    def clear_website(self):
        """
        Clears data related to specific website models if their corresponding database tables exist.
        """
        potential_models = [
            "blog.tag.category", "blog.tag", "blog.post", "blog.blog", "product.wishlist",
            "website.visitor", "website.redirect", "website.seo.metadata",
            "website.published.multi.mixin", "website.published.mixin", "website.multi.mixin"
        ]
        to_elimination = []
        for model in potential_models:
            if self.env["ir.model"]._get(model):
                model_obj = self.env[model]
                self._cr.execute(
                    f"""SELECT EXISTS (SELECT 1 FROM information_schema.tables
                    WHERE table_name = '{model_obj._table}' AND table_schema = current_schema())""")
                table_exists = self._cr.fetchone()[0]
                if table_exists:
                    to_elimination.append(model)
        if to_elimination:
            return self.data_elimination_with_transaction(to_elimination)

    def clear_message(self):
        """
        Clears specific data models by invoking the data elimination process.
        """
        to_elimination = ["mail.message", "mail.followers", "mail.activity"]
        return self.data_elimination_with_transaction(to_elimination)

    def clear_all(self):
        """
        Safely clears all transaction data while preserving master data.
        Makes sure critical system tables needed for settings aren't affected.
        """
        self._cr.rollback()
        # Define tables that contain transaction data by module
        transaction_tables = {
            'sale': ['sale_order', 'sale_order_line', 'sale_report'],
            'account': [
                'account_move', 'account_move_line', 'account_payment',
                'account_invoice_report'
            ],
            'point_of_sale': ['pos_order', 'pos_order_line', 'pos_session', 'pos_payment'],
            'purchase': ['purchase_order', 'purchase_order_line', 'purchase_report'],
            'hr_expense': ['hr_expense', 'hr_expense_sheet'],
            'stock': [
                'stock_move', 'stock_move_line', 'stock_picking',
                'stock_quant', 'stock_valuation_layer'
            ],
            'mrp': ['mrp_production', 'mrp_workorder'],
            'project': ['project_task', 'project_task_recurrence']
        }
        # Check which modules are installed
        installed_modules = self.env['ir.module.module'].search([
            ('state', '=', 'installed'),
            ('name', 'in', list(transaction_tables.keys()))
        ]).mapped('name')
        try:
            self._cr.rollback()
            self.clear_message()
            self._cr.commit()
        except psycopg2.DatabaseError:
            self._cr.rollback()
        for module in installed_modules:
            if module in transaction_tables:
                for table in transaction_tables[module]:
                    try:
                        if self._table_exists(table):
                            self._cr.execute(f"DELETE FROM {table};")
                            self._cr.commit()
                    except psycopg2.DatabaseError:
                        self._cr.rollback()
        # Reset sequences where needed
        try:
            self._reset_sequences()
            self._cr.commit()
        except psycopg2.DatabaseError:
            self._cr.rollback()
        return True

    def _table_exists(self, table_name):
        """Check if a table exists in the database."""
        self._cr.execute("""SELECT EXISTS ( SELECT FROM information_schema.tables
            WHERE table_name = %s)""", (table_name,))
        return self._cr.fetchone()[0]

    def _reset_sequences(self):
        """Reset sequences for common transaction tables."""
        sequence_queries = [
            "SELECT setval('sale_order_id_seq', 1, false);",
            "SELECT setval('account_move_id_seq', 1, false);",
            "SELECT setval('purchase_order_id_seq', 1, false);",
            "SELECT setval('stock_picking_id_seq', 1, false);",
            "SELECT setval('pos_order_id_seq', 1, false);"
        ]
        for query in sequence_queries:
            # Extract sequence name from the query
            seq_name = query.split("'")[1]
            # Check if the sequence exists before resetting it
            self._cr.execute(
                f"""SELECT EXISTS (SELECT 1 FROM pg_class WHERE relname =
                    '{seq_name}' AND relkind = 'S');""")
            exists = self._cr.fetchone()[0]
            if exists:
                self._cr.execute(query)

    def reset_category_location_name(self):
        """
        Resets the `complete_name` field for product categories and stock locations,
        and updates the `display_name` field for product templates and product variants
        in the database.
        """
        # Handle product categories
        self._cr.execute("""
            SELECT id FROM product_category WHERE parent_id IS NOT NULL
            ORDER BY complete_name""")
        category_ids = [r[0] for r in self._cr.fetchall()]
        batch_size = 1000
        for i in range(0, len(category_ids), batch_size):
            batch = category_ids[i: i + batch_size]
            categories = self.env["product.category"].sudo().browse(batch)
            for category in categories:
                parent_path = []
                current = category
                while current.parent_id:
                    parent_path.insert(0, current.parent_id.name)
                    current = current.parent_id
                complete_name = category.name
                if parent_path:
                    complete_name = " / ".join(parent_path + [category.name])
                self._cr.execute("""
                    UPDATE product_category SET complete_name = %s WHERE id = %s
                """, (complete_name, category.id))
            self._cr.commit()
        # Handle stock locations with sudo access
        if self.env["ir.model"]._get("stock.location"):
            self._cr.execute("""SELECT id FROM stock_location WHERE location_id IS NOT NULL
                ORDER BY complete_name""")
            location_ids = [r[0] for r in self._cr.fetchall()]
            for i in range(0, len(location_ids), batch_size):
                batch = location_ids[i: i + batch_size]
                locations = self.env["stock.location"].sudo().browse(batch)
                for location in locations:
                    parent_path = []
                    current = location
                    while current.location_id:
                        parent_path.insert(0, current.location_id.name)
                        current = current.location_id
                    complete_name = location.name
                    if parent_path:
                        complete_name = " / ".join(parent_path + [location.name])
                    self._cr.execute("""
                        UPDATE stock_location SET complete_name = %s WHERE id = %s""",
                        (complete_name, location.id))
                self._cr.commit()
        # Handle product template and variant display names
        if self.env["ir.model"]._get("product.template"):
            self._cr.execute(""" SELECT column_name FROM information_schema.columns
                WHERE table_name = 'product_template' AND column_name = 'display_name'""")
            if self._cr.fetchone():
                self._cr.execute("""UPDATE product_template SET display_name = name
                    WHERE display_name != name""")
            # Handle product variants
            self._cr.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'product_product' AND column_name = 'display_name'""")
            if self._cr.fetchone():
                self._cr.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'product_product' AND column_name = 'name_get_res'""")
                if self._cr.fetchone():
                    self._cr.execute("""
                        UPDATE product_product SET display_name = name_get_res
                        WHERE display_name != name_get_res""")
                else:
                    self._cr.execute("""
                        UPDATE product_product SET display_name = name
                        WHERE display_name != name""")
            self._cr.commit()
        return True
