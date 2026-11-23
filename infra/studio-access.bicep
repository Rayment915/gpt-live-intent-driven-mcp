targetScope = 'resourceGroup'

param principalId string
param registryName string = 'zhenweiacr'
param aiAccountName string = 'zhenw-mlzzscyb-eastus2'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: aiAccountName
}

var pullRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var inferenceRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')

resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, principalId, pullRole)
  scope: registry
  properties: {
    roleDefinitionId: pullRole
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

resource inference 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, inferenceRole)
  scope: account
  properties: {
    roleDefinitionId: inferenceRole
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
