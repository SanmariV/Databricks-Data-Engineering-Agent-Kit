select
    agent.Agent_BK,
    agent.AgentName,
    agent.BranchCode,
    agent.IsActive
from {{ ref('policy_admin.Agent') }} as agent
