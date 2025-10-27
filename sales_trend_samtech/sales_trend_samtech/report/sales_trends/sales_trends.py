# Copyright (c) 2025, Samtech and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import getdate

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
	data = []
	
	# Handle inactive customers
	if filters.get("based_on") == "Customer" and filters.get("inactive_customers"):
		return get_inactive_customers(filters, conditions)
	
	inc, cond = "", ""
	query_details = conditions["based_on_select"] + conditions["period_wise_select"]

	posting_date = "t1.transaction_date"
	if conditions.get("trans") in [
		"Sales Invoice",
		"Purchase Invoice",
		"Purchase Receipt",
		"Delivery Note",
	]:
		posting_date = "t1.posting_date"
		if filters.period_based_on and conditions.get("trans") in ["Sales Invoice", "Purchase Invoice"]:
			posting_date = "t1." + filters.period_based_on

	if conditions["based_on_select"] in ["t1.project,", "t2.project,"]:
		cond = " and " + conditions["based_on_select"][:-1] + " IS Not NULL"

	if not filters.get("include_closed_orders"):
		if conditions.get("trans") in ["Sales Order", "Purchase Order"]:
			cond += " and t1.status != 'Closed'"

	if conditions.get("trans") == "Quotation" and filters.get("group_by") == "Customer":
		cond += " and t1.quotation_to = 'Customer'"

	# Add industry filter
	if filters.get("industry") and filters.get("based_on") == "Customer":
		cond += " and t1.customer in (select name from `tabCustomer` where industry = %(industry)s)"
	
	# Add item filter
	if filters.get("item"):
		cond += " and t2.item_code = %(item)s"
	
	# Add item group filter
	if filters.get("item_group"):
		cond += " and t2.item_group = %(item_group)s"

	year_start_date, year_end_date = frappe.get_cached_value(
		"Fiscal Year", filters.get("fiscal_year"), ["year_start_date", "year_end_date"]
	)

	sql_params = {
		'company': filters.get("company"),
		'year_start_date': year_start_date,
		'year_end_date': year_end_date,
		'industry': filters.get("industry"),
		'item': filters.get("item"),
		'item_group': filters.get("item_group")
	}

	if filters.get("group_by"):
		sel_col = ""
		ind = conditions["columns"].index(conditions["grbc"][0])

		if filters.get("group_by") == "Item":
			sel_col = "t2.item_code"
		elif filters.get("group_by") == "Customer":
			sel_col = "t1.party_name" if conditions.get("trans") == "Quotation" else "t1.customer"
		elif filters.get("group_by") == "Supplier":
			sel_col = "t1.supplier"

		if filters.get("based_on") in ["Customer", "Supplier"]:
			inc = 3
		elif filters.get("based_on") in ["Item"]:
			inc = 2
		else:
			inc = 1

		data1 = frappe.db.sql(
			""" select {} from `tab{}` t1, `tab{} Item` t2 {}
					where t2.parent = t1.name and t1.company = %(company)s and {} between %(year_start_date)s and %(year_end_date)s and
					t1.docstatus = 1 {} {}
					group by {}
				""".format(
				query_details,
				conditions["trans"],
				conditions["trans"],
				conditions["addl_tables"],
				posting_date,
				conditions.get("addl_tables_relational_cond"),
				cond,
				conditions["group_by"],
			),
			sql_params,
			as_list=1,
		)

		for d in range(len(data1)):
			# to add blanck column
			dt = data1[d]
			dt.insert(ind, "")
			data.append(dt)

			# to get distinct value of col specified by group_by in filter
			row = frappe.db.sql(
				"""select DISTINCT({}) from `tab{}` t1, `tab{} Item` t2 {}
						where t2.parent = t1.name and t1.company = %(company)s and {} between %(year_start_date)s and %(year_end_date)s
						and t1.docstatus = 1 and {} = %(group_val)s {} {}
					""".format(
					sel_col,
					conditions["trans"],
					conditions["trans"],
					conditions["addl_tables"],
					posting_date,
					conditions["group_by"],
					conditions.get("addl_tables_relational_cond"),
					cond,
				),
				dict(sql_params, group_val=data1[d][0]),
				as_list=1,
			)

			for i in range(len(row)):
				des = ["" for q in range(len(conditions["columns"]))]

				# get data for group_by filter
				row1 = frappe.db.sql(
					""" select {} , {} from `tab{}` t1, `tab{} Item` t2 {}
							where t2.parent = t1.name and t1.company = %(company)s and {} between %(year_start_date)s and %(year_end_date)s
							and t1.docstatus = 1 and {} = %(sel_val)s and {} = %(group_val)s {} {}
						""".format(
						sel_col,
						conditions["period_wise_select"],
						conditions["trans"],
						conditions["trans"],
						conditions["addl_tables"],
						posting_date,
						sel_col,
						conditions["group_by"],
						conditions.get("addl_tables_relational_cond"),
						cond,
					),
					dict(sql_params, sel_val=row[i][0], group_val=data1[d][0]),
					as_list=1,
				)

				des[ind] = row[i][0]

				for j in range(1, len(conditions["columns"]) - inc):
					des[j + inc] = row1[0][j]

				data.append(des)
	else:
		data = frappe.db.sql(
			""" select {} from `tab{}` t1, `tab{} Item` t2 {}
					where t2.parent = t1.name and t1.company = %(company)s and {} between %(year_start_date)s and %(year_end_date)s and
					t1.docstatus = 1 {} {}
					group by {}
				""".format(
				query_details,
				conditions["trans"],
				conditions["trans"],
				conditions["addl_tables"],
				posting_date,
				cond,
				conditions.get("addl_tables_relational_cond", ""),
				conditions["group_by"],
			),
			sql_params,
			as_list=1,
		)

	return data


def get_inactive_customers(filters, conditions):
	"""Get customers with no transactions in the selected period"""
	year_start_date, year_end_date = frappe.get_cached_value(
		"Fiscal Year", filters.get("fiscal_year"), ["year_start_date", "year_end_date"]
	)
	
	posting_date = "posting_date"
	if filters.period_based_on:
		posting_date = filters.period_based_on
	
	# Build item conditions
	item_cond = ""
	if filters.get("item"):
		item_cond += " and si_item.item_code = %(item)s"
	if filters.get("item_group"):
		item_cond += " and si_item.item_group = %(item_group)s"
	
	# Get all customers
	customer_filters = {"disabled": 0}
	if filters.get("industry"):
		customer_filters["industry"] = filters.get("industry")
	
	all_customers = frappe.get_all(
		"Customer",
		filters=customer_filters,
		fields=["name", "customer_name", "territory"]
	)
	
	# Get customers with transactions
	active_customers = frappe.db.sql("""
		SELECT DISTINCT si.customer
		FROM `tabSales Invoice` si
		INNER JOIN `tabSales Invoice Item` si_item ON si_item.parent = si.name
		WHERE si.company = %(company)s
		AND si.{posting_date} BETWEEN %(year_start_date)s AND %(year_end_date)s
		AND si.docstatus = 1
		{item_cond}
	""".format(posting_date=posting_date, item_cond=item_cond), {
		'company': filters.get("company"),
		'year_start_date': year_start_date,
		'year_end_date': year_end_date,
		'item': filters.get("item"),
		'item_group': filters.get("item_group")
	}, as_dict=1)
	
	active_customer_set = {c.customer for c in active_customers}
	
	# Get inactive customers
	inactive_customers = [c for c in all_customers if c.name not in active_customer_set]
	
	# Build result with zeros for all periods
	data = []
	num_periods = len(conditions["columns"]) - 4  # Subtract: Customer, Name, Territory, Total
	
	for customer in inactive_customers:
		row = [
			customer.name,
			customer.customer_name,
			customer.territory
		]
		# Add zeros for all period columns
		row.extend([0.0] * num_periods)
		# Add total
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

	# based_on_cols, based_on_select, based_on_group_by, addl_tables
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