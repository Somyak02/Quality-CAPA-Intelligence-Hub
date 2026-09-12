from quality_workflow import QualityWorkflow


if __name__ == "__main__":
    workflow = QualityWorkflow()
    sample_ticket = workflow.ticket_df.iloc[0].to_dict()
    result = workflow.process_ticket(sample_ticket)
    print("Ticket:", result["ticket"]["ticket_id"])
    print("Severity:", result["severity"])
    print("Category:", result["category"])
    print("Status:", result["status"])
    print("Draft preview:")
    print(result["draft"][:500])
    print("Metrics:", workflow.get_metrics())
