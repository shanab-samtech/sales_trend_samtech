# Copyright (c) 2025, Samtech and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import getdate
from frappe.query_builder import DocType, Case
from frappe.query_builder.functions import Sum, Count
from pypika import Order

def execute(filters=None):
	if not filters:
		filters = {}
	data = []
	conditions = get_columns(filters, "Sales Invoice")
	data = get_data(filters, conditions)

	return conditions["columns"], data

def get_columns(filters, trans):
	validate_filters(filters)

	# get conditions for based_on filter cond
	based_on_details = based_wise_columns_query(filters.get("based_on"), trans)
	# get conditions for periodic filter cond
	period_cols, period_select = period_wise_columns_query(filters, trans)
	# get conditions for grouping filter cond
	group_by_cols = group_wise_column(filters.get("group_by"))

	columns = (
		based_on_details["based_on_cols"]
		+ period_cols
		+ [_("Total(Qty)") + ":Float:120"]
	)
	if group_by_cols:
		columns = (
			based_on_details["based_on_cols"]
			+ group_by_cols
			+ period_cols
			+ [_("Total(Qty)") + ":Float:120"]
		)

	conditions = {
		"based_on_select": based_on_details["based_on_select"],
		"period_wise_select": period_select,
		"columns": columns,
		"group_by": based_on_details["based_on_group_by"],
		"grbc": group_by_cols,
		"trans": trans,
		"addl_tables": based_on_details["addl_tables"],
		"addl_tables_relational_cond": based_on_details.get("addl_tables_relational_cond", ""),
	}

	return conditions


def validate_filters(filters):
	for f in ["Fiscal Year", "Based On", "Period", "Company"]:
		if not filters.get(f.lower().replace(" ", "_")):
			frappe.throw(_("{0} is mandatory").format(_(f)))

	if not frappe.db.exists("Fiscal Year", filters.get("fiscal_year")):
		frappe.throw(_("Fiscal Year {0} Does Not Exist").format(filters.get("fiscal_year")))

	if filters.get("based_on") == filters.get("group_by"):
		frappe.throw(_("'Based On' and 'Group By' can not be same"))


def get_data(filters, conditions):
    """
    Fetch sales trend data using Frappe ORM to respect user permissions.
    This replaces raw SQL queries to ensure proper permission enforcement.
    """
    data = []

    # Handle inactive customers branch first
    if filters.get("based_on") == "Customer" and filters.get("inactive_customers"):
        return get_inactive_customers(filters, conditions)

    trans = conditions.get("trans")
    year_start_date, year_end_date = frappe.get_cached_value(
        "Fiscal Year", filters.get("fiscal_year"), ["year_start_date", "year_end_date"]
    )

    # Determine the posting date field
    posting_date_field = get_posting_date_field(filters, trans)

    # Build base filters for frappe.get_all()
    base_filters = build_base_filters(filters, conditions, year_start_date, year_end_date, posting_date_field)

    # Get all matching parent documents with user permissions applied
    parent_docs = get_parent_documents(trans, base_filters, filters, conditions)

    if not parent_docs:
        return data

    # Get period ranges for time-based aggregation
    period_ranges = get_period_date_ranges(filters.get("period"), filters.get("fiscal_year"))

    # Process data based on grouping requirements
    if filters.get("group_by"):
        data = process_grouped_data(parent_docs, filters, conditions, period_ranges, posting_date_field, trans)
    else:
        data = process_ungrouped_data(parent_docs, filters, conditions, period_ranges, posting_date_field, trans)

    return data


def get_posting_date_field(filters, trans):
    """Determine which date field to use for the transaction"""
    if trans in ["Sales Invoice", "Purchase Invoice", "Purchase Receipt", "Delivery Note"]:
        if filters.get("period_based_on") and trans in ["Sales Invoice", "Purchase Invoice"]:
            return filters.period_based_on
        return "posting_date"
    return "transaction_date"


def build_base_filters(filters, conditions, year_start_date, year_end_date, posting_date_field):
    """Build base filters for querying parent documents"""
    base_filters = {
        "company": filters.get("company"),
        "docstatus": 1,
        posting_date_field: ["between", [year_start_date, year_end_date]]
    }

    trans = conditions.get("trans")

    # Add status filter for orders
    if not filters.get("include_closed_orders"):
        if trans in ["Sales Order", "Purchase Order"]:
            base_filters["status"] = ["!=", "Closed"]

    # Add quotation-specific filter
    if trans == "Quotation" and filters.get("group_by") == "Customer":
        base_filters["quotation_to"] = "Customer"

    return base_filters


def get_parent_documents(trans, base_filters, filters, conditions):
    """
    Get parent transaction documents using frappe.get_all()
    which automatically applies user permissions.
    """
    fields = ["name", "company"]

    # Add fields based on transaction type and filters
    if trans == "Quotation":
        fields.extend(["party_name", "customer_name", "territory", "customer_group"])
    elif trans in ["Sales Invoice", "Delivery Note", "Sales Order"]:
        fields.extend(["customer", "customer_name", "territory", "customer_group", "project"])
    elif trans in ["Purchase Order", "Purchase Invoice", "Purchase Receipt"]:
        fields.extend(["supplier", "supplier_name"])

    # Get all parent documents with user permissions enforced
    parent_docs = frappe.get_all(
        trans,
        filters=base_filters,
        fields=fields,
        limit_page_length=0
    )

    if not parent_docs:
        return []

    # Apply industry filter if specified
    if filters.get("industry") and filters.get("based_on") == "Customer":
        parent_docs = filter_by_industry(parent_docs, filters.get("industry"), trans)

    # Get parent document names for child item query
    parent_names = [doc.name for doc in parent_docs]

    # Get child items with user permissions
    item_child_doctype = f"{trans} Item"
    item_filters = {"parent": ["in", parent_names]}

    if filters.get("item"):
        item_filters["item_code"] = filters.get("item")
    if filters.get("item_group"):
        item_filters["item_group"] = filters.get("item_group")

    # Apply project filter for purchase items
    if conditions.get("based_on_select") == "t2.project,":
        item_filters["project"] = ["is", "set"]

    item_fields = ["parent", "item_code", "item_name", "item_group", "stock_qty", "project"]

    child_items = frappe.get_all(
        item_child_doctype,
        filters=item_filters,
        fields=item_fields,
        limit_page_length=0
    )

    # Enrich parent documents with their items
    parent_dict = {doc.name: doc for doc in parent_docs}
    for parent_doc in parent_dict.values():
        parent_doc.items = []

    for item in child_items:
        if item.parent in parent_dict:
            parent_dict[item.parent].items.append(item)

    # Filter out parents with no matching items
    parent_docs = [doc for doc in parent_docs if doc.get("items")]

    # Enrich with supplier group if needed
    if conditions.get("addl_tables") and "tabSupplier" in conditions["addl_tables"]:
        enrich_with_supplier_group(parent_docs)

    return parent_docs


def filter_by_industry(parent_docs, industry, trans):
    """Filter customers by industry using ORM"""
    # Get customers that have the specified industry
    industry_customers = frappe.get_all(
        "Industry Type Detail",
        filters={
            "parenttype": "Customer",
            "industry_type_detail": industry
        },
        fields=["parent"],
        pluck="parent"
    )

    industry_customer_set = set(industry_customers)

    # Filter parent docs to only include those with matching industry
    filtered_docs = []
    for doc in parent_docs:
        customer_field = "party_name" if trans == "Quotation" else "customer"
        if hasattr(doc, customer_field) and getattr(doc, customer_field) in industry_customer_set:
            filtered_docs.append(doc)

    return filtered_docs


def enrich_with_supplier_group(parent_docs):
    """Add supplier_group to parent documents"""
    supplier_names = list(set([doc.supplier for doc in parent_docs if hasattr(doc, "supplier")]))
    if supplier_names:
        suppliers = frappe.get_all(
            "Supplier",
            filters={"name": ["in", supplier_names]},
            fields=["name", "supplier_group"]
        )
        supplier_map = {s.name: s.supplier_group for s in suppliers}
        for doc in parent_docs:
            if hasattr(doc, "supplier"):
                doc.supplier_group = supplier_map.get(doc.supplier, "")


def process_ungrouped_data(parent_docs, filters, conditions, period_ranges, posting_date_field, trans):
    """Process data without group_by aggregation"""
    data = []
    based_on = filters.get("based_on")

    # Group documents by the "based_on" field
    grouped_data = {}

    for parent_doc in parent_docs:
        # For Item and Item Group, we group by item-level fields
        if based_on == "Item":
            for item in parent_doc.items:
                based_on_key = item.item_code
                if based_on_key not in grouped_data:
                    grouped_data[based_on_key] = {
                        "item_info": item,
                        "items": [],
                    }
                grouped_data[based_on_key]["items"].append({
                    "item": item,
                    "parent_date": getattr(parent_doc, posting_date_field)
                })
        elif based_on == "Item Group":
            for item in parent_doc.items:
                based_on_key = item.item_group
                if based_on_key not in grouped_data:
                    grouped_data[based_on_key] = {
                        "item_group": item.item_group,
                        "items": [],
                    }
                grouped_data[based_on_key]["items"].append({
                    "item": item,
                    "parent_date": getattr(parent_doc, posting_date_field)
                })
        else:
            # For parent-level fields (Customer, Territory, etc.)
            based_on_key = get_based_on_value(parent_doc, based_on, trans)

            if based_on_key not in grouped_data:
                grouped_data[based_on_key] = {
                    "parent_doc": parent_doc,
                    "items": [],
                }

            # Add items to this group
            for item in parent_doc.items:
                grouped_data[based_on_key]["items"].append({
                    "item": item,
                    "parent_date": getattr(parent_doc, posting_date_field)
                })

    # Calculate period-wise quantities
    for key, group_info in grouped_data.items():
        # Build row based on based_on type
        if based_on == "Item":
            row = [group_info["item_info"].item_code, group_info["item_info"].item_name]
        elif based_on == "Item Group":
            row = [group_info["item_group"]]
        else:
            row = build_row_from_based_on(group_info["parent_doc"], based_on, trans)

        total_qty = 0.0

        if filters.get("period") != "Yearly":
            period_qtys = [0.0] * len(period_ranges)
            for item_data in group_info["items"]:
                item = item_data["item"]
                parent_date = item_data["parent_date"]
                qty = item.stock_qty or 0.0
                total_qty += qty

                # Find which period this belongs to
                for idx, (start_date, end_date) in enumerate(period_ranges):
                    if start_date <= parent_date <= end_date:
                        period_qtys[idx] += qty
                        break

            row.extend(period_qtys)
        else:
            # Yearly - just sum all
            for item_data in group_info["items"]:
                total_qty += item_data["item"].stock_qty or 0.0
            row.append(total_qty)

        row.append(total_qty)
        data.append(row)

    return data


def process_grouped_data(parent_docs, filters, conditions, period_ranges, posting_date_field, trans):
    """Process data with group_by aggregation"""
    data = []
    based_on = filters.get("based_on")
    group_by = filters.get("group_by")

    # Multi-level grouping: first by based_on, then by group_by
    grouped_data = {}

    for parent_doc in parent_docs:
        # Handle Item and Item Group based_on
        if based_on == "Item":
            for item in parent_doc.items:
                based_on_key = item.item_code
                if based_on_key not in grouped_data:
                    grouped_data[based_on_key] = {
                        "item_info": item,
                        "subgroups": {}
                    }

                # Now group by the group_by field
                group_by_key = get_group_by_value(parent_doc, item, group_by, trans)

                if group_by_key not in grouped_data[based_on_key]["subgroups"]:
                    grouped_data[based_on_key]["subgroups"][group_by_key] = []

                grouped_data[based_on_key]["subgroups"][group_by_key].append({
                    "item": item,
                    "parent_date": getattr(parent_doc, posting_date_field)
                })

        elif based_on == "Item Group":
            for item in parent_doc.items:
                based_on_key = item.item_group
                if based_on_key not in grouped_data:
                    grouped_data[based_on_key] = {
                        "item_group": item.item_group,
                        "subgroups": {}
                    }

                # Now group by the group_by field
                group_by_key = get_group_by_value(parent_doc, item, group_by, trans)

                if group_by_key not in grouped_data[based_on_key]["subgroups"]:
                    grouped_data[based_on_key]["subgroups"][group_by_key] = []

                grouped_data[based_on_key]["subgroups"][group_by_key].append({
                    "item": item,
                    "parent_date": getattr(parent_doc, posting_date_field)
                })

        else:
            # For parent-level fields (Customer, Territory, etc.)
            based_on_key = get_based_on_value(parent_doc, based_on, trans)

            if based_on_key not in grouped_data:
                grouped_data[based_on_key] = {
                    "parent_doc": parent_doc,
                    "subgroups": {}
                }

            # Now group by the group_by field
            for item in parent_doc.items:
                group_by_key = get_group_by_value(parent_doc, item, group_by, trans)

                if group_by_key not in grouped_data[based_on_key]["subgroups"]:
                    grouped_data[based_on_key]["subgroups"][group_by_key] = []

                grouped_data[based_on_key]["subgroups"][group_by_key].append({
                    "item": item,
                    "parent_date": getattr(parent_doc, posting_date_field)
                })

    # Determine column index for group_by insertion
    ind = conditions["columns"].index(conditions["grbc"][0])

    if based_on in ["Customer", "Supplier"]:
        inc = 3
    elif based_on in ["Item"]:
        inc = 2
    else:
        inc = 1

    # Build output rows
    for based_on_key, group_info in grouped_data.items():
        # First add the summary row for this based_on group
        if based_on == "Item":
            summary_row = [group_info["item_info"].item_code, group_info["item_info"].item_name]
        elif based_on == "Item Group":
            summary_row = [group_info["item_group"]]
        else:
            summary_row = build_row_from_based_on(group_info["parent_doc"], based_on, trans)

        # Calculate totals across all subgroups
        if filters.get("period") != "Yearly":
            summary_period_qtys = [0.0] * len(period_ranges)
        else:
            summary_period_qtys = [0.0]

        summary_total = 0.0

        for group_by_key, items_data in group_info["subgroups"].items():
            for item_data in items_data:
                qty = item_data["item"].stock_qty or 0.0
                summary_total += qty

                if filters.get("period") != "Yearly":
                    parent_date = item_data["parent_date"]
                    for idx, (start_date, end_date) in enumerate(period_ranges):
                        if start_date <= parent_date <= end_date:
                            summary_period_qtys[idx] += qty
                            break
                else:
                    summary_period_qtys[0] += qty

        summary_row.extend(summary_period_qtys)
        summary_row.append(summary_total)
        summary_row.insert(ind, "")  # Empty slot for group_by column
        data.append(summary_row)

        # Now add detail rows for each subgroup
        for group_by_key, items_data in group_info["subgroups"].items():
            detail_row = [""] * len(conditions["columns"])
            detail_row[ind] = group_by_key

            if filters.get("period") != "Yearly":
                period_qtys = [0.0] * len(period_ranges)
            else:
                period_qtys = [0.0]

            detail_total = 0.0

            for item_data in items_data:
                qty = item_data["item"].stock_qty or 0.0
                detail_total += qty

                if filters.get("period") != "Yearly":
                    parent_date = item_data["parent_date"]
                    for idx, (start_date, end_date) in enumerate(period_ranges):
                        if start_date <= parent_date <= end_date:
                            period_qtys[idx] += qty
                            break
                else:
                    period_qtys[0] += qty

            for j in range(len(period_qtys)):
                detail_row[j + inc] = period_qtys[j]
            detail_row[-1] = detail_total

            data.append(detail_row)

    return data


def get_based_on_value(parent_doc, based_on, trans):
    """Get the value for the based_on field from parent document"""
    if based_on == "Item":
        # For item-based reports, we'll handle this differently
        return None
    elif based_on == "Item Group":
        return None  # Handled via items
    elif based_on == "Customer":
        return parent_doc.party_name if trans == "Quotation" else parent_doc.customer
    elif based_on == "Customer Group":
        return parent_doc.customer_group
    elif based_on == "Supplier":
        return parent_doc.supplier
    elif based_on == "Supplier Group":
        return parent_doc.supplier_group
    elif based_on == "Territory":
        return parent_doc.territory
    elif based_on == "Project":
        return parent_doc.project if hasattr(parent_doc, "project") else None
    return None


def build_row_from_based_on(parent_doc, based_on, trans):
    """Build the initial part of a row based on the based_on field"""
    row = []

    if based_on == "Customer":
        if trans == "Quotation":
            row = [parent_doc.party_name, parent_doc.customer_name, parent_doc.territory]
        else:
            row = [parent_doc.customer, parent_doc.customer_name, parent_doc.territory]
    elif based_on == "Customer Group":
        row = [parent_doc.customer_group]
    elif based_on == "Supplier":
        row = [parent_doc.supplier, parent_doc.supplier_name, parent_doc.supplier_group]
    elif based_on == "Supplier Group":
        row = [parent_doc.supplier_group]
    elif based_on == "Territory":
        row = [parent_doc.territory]
    elif based_on == "Project":
        project = parent_doc.project if hasattr(parent_doc, "project") else ""
        row = [project]

    return row


def get_group_by_value(parent_doc, item, group_by, trans):
    """Get the value for the group_by field"""
    if group_by == "Item":
        return item.item_code
    elif group_by == "Customer":
        return parent_doc.party_name if trans == "Quotation" else parent_doc.customer
    elif group_by == "Supplier":
        return parent_doc.supplier
    return None


def get_inactive_customers(filters, conditions):
    """
    Get inactive customers using Frappe ORM to respect user permissions.
    This replaces raw SQL to ensure proper permission enforcement.
    """
    year_start_date, year_end_date = frappe.get_cached_value(
        "Fiscal Year", filters.get("fiscal_year"), ["year_start_date", "year_end_date"]
    )

    posting_date_field = "posting_date"
    if filters.get("period_based_on"):
        posting_date_field = filters.period_based_on

    # Get all permitted customers with user permissions enforced
    customer_filters = {"disabled": 0}

    if filters.get("industry"):
        # Get customers that have the specified industry using ORM
        industry_customers = frappe.get_all(
            "Industry Type Detail",
            filters={
                "parenttype": "Customer",
                "industry_type_detail": filters.get("industry")
            },
            pluck="parent"
        )
        # Add industry filter to customer query
        customer_filters["name"] = ["in", industry_customers]

    all_customers = frappe.get_list(
        "Customer",
        filters=customer_filters,
        fields=["name", "customer_name", "territory"],
        limit_page_length=0,
    )

    if not all_customers:
        return []

    # Get list of permitted customer names
    permitted_customer_names = [c["name"] for c in all_customers]

    # Get active customers using frappe.get_all() which respects permissions
    # Only check within the permitted customers list
    active_customers_data = frappe.get_all(
        "Sales Invoice",
        filters={
            "company": filters.get("company"),
            posting_date_field: ["between", [year_start_date, year_end_date]],
            "docstatus": 1,
            "customer": ["in", permitted_customer_names]
        },
        fields=["customer"],
        distinct=True,
        limit_page_length=0
    )

    active_customer_set = {c.customer for c in active_customers_data}
    inactive_customers = [c for c in all_customers if c["name"] not in active_customer_set]

    data = []
    num_periods = len(conditions["columns"]) - 4  # Customer, Name, Territory, Total

    for customer in inactive_customers:
        row = [customer["name"], customer["customer_name"], customer["territory"]]
        row.extend([0.0] * num_periods)
        row.append(0.0)
        data.append(row)

    return data



def get_mon(dt):
	return getdate(dt).strftime("%b")


def period_wise_columns_query(filters, trans):
	query_details = ""
	pwc = []
	bet_dates = get_period_date_ranges(filters.get("period"), filters.get("fiscal_year"))

	if trans in ["Purchase Receipt", "Delivery Note", "Purchase Invoice", "Sales Invoice"]:
		trans_date = "posting_date"
		if filters.period_based_on and trans in ["Purchase Invoice", "Sales Invoice"]:
			trans_date = filters.period_based_on
	else:
		trans_date = "transaction_date"

	if filters.get("period") != "Yearly":
		for dt in bet_dates:
			get_period_wise_columns(dt, filters.get("period"), pwc)
			query_details = get_period_wise_query(dt, trans_date, query_details)
	else:
		pwc = [
			_(filters.get("fiscal_year")) + " (" + _("Qty") + "):Float:120"
		]
		query_details = " SUM(t2.stock_qty),"

	query_details += "SUM(t2.stock_qty)"
	return pwc, query_details


def get_period_wise_columns(bet_dates, period, pwc):
	if period == "Monthly":
		pwc += [
			_(get_mon(bet_dates[0])) + " (" + _("Qty") + "):Float:120"
		]
	else:
		pwc += [
			_(get_mon(bet_dates[0])) + "-" + _(get_mon(bet_dates[1])) + " (" + _("Qty") + "):Float:120"
		]


def get_period_wise_query(bet_dates, trans_date, query_details):
	query_details += """SUM(IF(t1.{trans_date} BETWEEN '{sd}' AND '{ed}', t2.stock_qty, NULL)),
				""".format(
		trans_date=trans_date,
		sd=bet_dates[0],
		ed=bet_dates[1],
	)
	return query_details


@frappe.whitelist(allow_guest=True)
def get_period_date_ranges(period, fiscal_year=None, year_start_date=None):
	from dateutil.relativedelta import relativedelta

	if not year_start_date:
		year_start_date, year_end_date = frappe.get_cached_value(
			"Fiscal Year", fiscal_year, ["year_start_date", "year_end_date"]
		)

	increment = {"Monthly": 1, "Quarterly": 3, "Half-Yearly": 6, "Yearly": 12}.get(period)

	period_date_ranges = []
	for _i in range(1, 13, increment):
		period_end_date = getdate(year_start_date) + relativedelta(months=increment, days=-1)
		if period_end_date > getdate(year_end_date):
			period_end_date = year_end_date
		period_date_ranges.append([year_start_date, period_end_date])
		year_start_date = period_end_date + relativedelta(days=1)
		if period_end_date == year_end_date:
			break

	return period_date_ranges


def get_period_month_ranges(period, fiscal_year):
	from dateutil.relativedelta import relativedelta

	period_month_ranges = []

	for start_date, end_date in get_period_date_ranges(period, fiscal_year):
		months_in_this_period = []
		while start_date <= end_date:
			months_in_this_period.append(start_date.strftime("%B"))
			start_date += relativedelta(months=1)
		period_month_ranges.append(months_in_this_period)

	return period_month_ranges


def based_wise_columns_query(based_on, trans):
	based_on_details = {}

	if based_on == "Item":
		based_on_details["based_on_cols"] = ["Item:Link/Item:120", "Item Name:Data:120"]
		based_on_details["based_on_select"] = "t2.item_code, t2.item_name,"
		based_on_details["based_on_group_by"] = "t2.item_code"
		based_on_details["addl_tables"] = ""

	elif based_on == "Item Group":
		based_on_details["based_on_cols"] = ["Item Group:Link/Item Group:120"]
		based_on_details["based_on_select"] = "t2.item_group,"
		based_on_details["based_on_group_by"] = "t2.item_group"
		based_on_details["addl_tables"] = ""

	elif based_on == "Customer":
		if trans == "Quotation":
			based_on_details["based_on_cols"] = [
				"Party:Link/Customer:120",
				"Party Name:Data:120",
				"Territory:Link/Territory:120",
			]
			based_on_details["based_on_select"] = "t1.party_name, t1.customer_name, t1.territory,"
		else:
			based_on_details["based_on_cols"] = [
				"Customer:Link/Customer:120",
				"Customer Name:Data:120",
				"Territory:Link/Territory:120",
			]
			based_on_details["based_on_select"] = "t1.customer, t1.customer_name, t1.territory,"
		based_on_details["based_on_group_by"] = "t1.party_name" if trans == "Quotation" else "t1.customer"
		based_on_details["addl_tables"] = ""

	elif based_on == "Customer Group":
		based_on_details["based_on_cols"] = ["Customer Group:Link/Customer Group"]
		based_on_details["based_on_select"] = "t1.customer_group,"
		based_on_details["based_on_group_by"] = "t1.customer_group"
		based_on_details["addl_tables"] = ""

	elif based_on == "Supplier":
		based_on_details["based_on_cols"] = [
			"Supplier:Link/Supplier:120",
			"Supplier Name:Data:120",
			"Supplier Group:Link/Supplier Group:140",
		]
		based_on_details["based_on_select"] = "t1.supplier, t1.supplier_name, t3.supplier_group,"
		based_on_details["based_on_group_by"] = "t1.supplier"
		based_on_details["addl_tables"] = ",`tabSupplier` t3"
		based_on_details["addl_tables_relational_cond"] = " and t1.supplier = t3.name"

	elif based_on == "Supplier Group":
		based_on_details["based_on_cols"] = ["Supplier Group:Link/Supplier Group:140"]
		based_on_details["based_on_select"] = "t3.supplier_group,"
		based_on_details["based_on_group_by"] = "t3.supplier_group"
		based_on_details["addl_tables"] = ",`tabSupplier` t3"
		based_on_details["addl_tables_relational_cond"] = " and t1.supplier = t3.name"

	elif based_on == "Territory":
		based_on_details["based_on_cols"] = ["Territory:Link/Territory:120"]
		based_on_details["based_on_select"] = "t1.territory,"
		based_on_details["based_on_group_by"] = "t1.territory"
		based_on_details["addl_tables"] = ""

	elif based_on == "Project":
		if trans in ["Sales Invoice", "Delivery Note", "Sales Order"]:
			based_on_details["based_on_cols"] = ["Project:Link/Project:120"]
			based_on_details["based_on_select"] = "t1.project,"
			based_on_details["based_on_group_by"] = "t1.project"
			based_on_details["addl_tables"] = ""
		elif trans in ["Purchase Order", "Purchase Invoice", "Purchase Receipt"]:
			based_on_details["based_on_cols"] = ["Project:Link/Project:120"]
			based_on_details["based_on_select"] = "t2.project,"
			based_on_details["based_on_group_by"] = "t2.project"
			based_on_details["addl_tables"] = ""
		else:
			frappe.throw(_("Project-wise data is not available for Quotation"))

	return based_on_details


def group_wise_column(group_by):
	if group_by:
		return [group_by + ":Link/" + group_by + ":120"]
	else:
		return []