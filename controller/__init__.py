"""Managed-autonomy controller (VovaElkTver fork). Off by default: config `controller.enabled`.
Guarantees held by code, not prompt: task/contract storage, transitions, permission epochs,
dispatch barrier, budgets, restart reconciliation. See plugins/controller for the hook surface.
"""
