targetScope = 'resourceGroup'

param location string = 'eastus2'
param environmentName string = 'cae-gpt-live-studio'
param identityName string = 'id-gpt-live-studio'
param workspaceName string = 'log-live-ztbtl5p2muqfk'
param reuseEnvironment bool = true

resource sharedEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

module environment 'br/public:avm/res/app/managed-environment:0.17.0' = if (!reuseEnvironment) {
  name: 'studio-environment'
  params: {
    name: environmentName
    location: location
    enableTelemetry: false
    internal: false
    publicNetworkAccess: 'Enabled'
    zoneRedundant: false
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsWorkspaceResourceId: workspace.id
    }
    tags: {
      project: 'gpt-live-python'
    }
  }
}

module identity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'studio-identity'
  params: {
    name: identityName
    location: location
    enableTelemetry: false
    tags: {
      project: 'gpt-live-python'
    }
  }
}

output environmentId string = reuseEnvironment ? sharedEnvironment.id : environment!.outputs.resourceId
output defaultDomain string = reuseEnvironment ? sharedEnvironment.properties.defaultDomain : environment!.outputs.defaultDomain
output identityId string = identity.outputs.resourceId
output principalId string = identity.outputs.principalId
output clientId string = identity.outputs.clientId
