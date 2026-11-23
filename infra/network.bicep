// Official self-hosted MCP sample VNet/private-endpoint pattern, shared across the three services.
param location string
param token string
param tags object

module network 'br/public:avm/res/network/virtual-network:0.6.1' = {
  name: 'private-storage-network'
  params: {
    name: 'vnet-live-${token}'
    location: location
    tags: tags
    addressPrefixes: ['10.44.0.0/16']
    subnets: [
      {
        name: 'private-endpoints'
        addressPrefix: '10.44.1.0/24'
        privateEndpointNetworkPolicies: 'Disabled'
        privateLinkServiceNetworkPolicies: 'Enabled'
      }
      {
        name: 'functions'
        addressPrefix: '10.44.2.0/24'
        delegation: 'Microsoft.App/environments'
        privateEndpointNetworkPolicies: 'Disabled'
        privateLinkServiceNetworkPolicies: 'Enabled'
      }
    ]
  }
}
module blobDns 'br/public:avm/res/network/private-dns-zone:0.7.1' = {
  name: 'blob-dns'
  params: {
    name: 'privatelink.blob.${environment().suffixes.storage}'
    location: 'global'
    tags: tags
    virtualNetworkLinks: [
      {
        name: 'functions'
        virtualNetworkResourceId: network.outputs.resourceId
        registrationEnabled: false
      }
    ]
  }
}
module tableDns 'br/public:avm/res/network/private-dns-zone:0.7.1' = {
  name: 'table-dns'
  params: {
    name: 'privatelink.table.${environment().suffixes.storage}'
    location: 'global'
    tags: tags
    virtualNetworkLinks: [
      {
        name: 'functions'
        virtualNetworkResourceId: network.outputs.resourceId
        registrationEnabled: false
      }
    ]
  }
}
output appSubnetId string = '${network.outputs.resourceId}/subnets/functions'
output privateSubnetId string = '${network.outputs.resourceId}/subnets/private-endpoints'
output blobDnsZoneId string = blobDns.outputs.resourceId
output tableDnsZoneId string = tableDns.outputs.resourceId
