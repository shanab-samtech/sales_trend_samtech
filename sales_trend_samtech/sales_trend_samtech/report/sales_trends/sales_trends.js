// Copyright (c) 2025, Samtech and contributors
// For license information, please see license.txt

frappe.query_reports["Sales Trends"] = $.extend({}, erpnext.sales_trends_filters);

frappe.query_reports["Sales Trends"]["filters"].push(
	{
		fieldname: "inactive_customers",
		label: __("Show Inactive Customers"),
		fieldtype: "Check",
		default: 0,
		depends_on: "eval:doc.based_on=='Customer'",
	},
	{
		fieldname: "industry",
		label: __("Industry"),
		fieldtype: "Link",
		options: "Industry Type",
		depends_on: "eval:doc.based_on=='Customer'",
	},
	{
		fieldname: "item",
		label: __("Item"),
		fieldtype: "Link",
		options: "Item",
		depends_on: "eval:doc.based_on=='Customer'",
	},
	{
		fieldname: "item_group",
		label: __("Item Group"),
		fieldtype: "Link",
		options: "Item Group",
		depends_on: "eval:doc.based_on=='Customer'",
	}
);
