targetScope = 'subscription'

param location string = 'eastus2'
param resourceGroupName string = 'rg-gpt-live-python-dev'
param sharedResourceGroupName string = 'rg1'
param deployApp bool = true
param reuseEnvironment bool = true
param appName string = 'ca-gpt-live-python'
param image string = 'zhenweiacr.azurecr.io/gpt-live-python/studio:pending'
param azureEndpoint string
param authUsername string = 'admin'

@secure()
param passwordHash string

param mcpFaqUrl string
param mcpRepairUrl string
param mcpVerificationUrl string

@secure()
param mcpFaqKey string

@secure()
param mcpRepairKey string

@secure()
param mcpVerificationKey string

resource group 'Microsoft.Resources/resourceGroups@2021-04-01' existing = {
  name: resourceGroupName
}

module foundation './studio-foundation.bicep' = {
  name: 'studio-foundation'
  scope: group
  params: {
    location: location
    reuseEnvironment: reuseEnvironment
  }
}

module access './studio-access.bicep' = {
  name: 'studio-access'
  scope: resourceGroup(sharedResourceGroupName)
  params: { principalId: foundation.outputs.principalId }
}

module application './studio-app.bicep' = if (deployApp) {
  name: 'studio-application'
  scope: group
  dependsOn: [access]
  params: {
    location: location
    appName: appName
    environmentId: foundation.outputs.environmentId
    defaultDomain: foundation.outputs.defaultDomain
    identityId: foundation.outputs.identityId
    clientId: foundation.outputs.clientId
    image: image
    azureEndpoint: azureEndpoint
    authUsername: authUsername
    passwordHash: passwordHash
    mcpFaqUrl: mcpFaqUrl
    mcpRepairUrl: mcpRepairUrl
    mcpVerificationUrl: mcpVerificationUrl
    mcpFaqKey: mcpFaqKey
    mcpRepairKey: mcpRepairKey
    mcpVerificationKey: mcpVerificationKey
  }
}

output environmentId string = foundation.outputs.environmentId
output identityId string = foundation.outputs.identityId
output clientId string = foundation.outputs.clientId
output url string = deployApp ? application!.outputs.url : ''
