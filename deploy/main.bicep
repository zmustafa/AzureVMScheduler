targetScope = 'resourceGroup'

@description('Azure region for all resources. Defaults to westus3, which is validated for PostgreSQL Flexible Server B1ms and Azure Container Apps.')
param location string = 'westus3'

@description('Base name for the Azure VM Scheduler deployment. Use lowercase letters, numbers, and hyphens.')
@minLength(3)
@maxLength(40)
param appName string = 'azure-vm-scheduler'

@description('Container image to deploy. Defaults to the public Docker Hub image, :latest tag, so the one-click button always provisions the newest published release.')
param containerImage string = 'docker.io/zmustafa/azure-vm-scheduler:latest'

@description('Private registry login server (for example myregistry.azurecr.io). Leave empty to pull the public image anonymously.')
param registryServer string = ''

@description('Username for the private registry. Ignored when Registry server is empty.')
param registryUsername string = ''

@description('Password or token for the private registry. Ignored when Registry server is empty.')
@secure()
param registryPassword string = ''

var usePrivateRegistry = !empty(registryServer)

@description('Bootstrap local admin username for first login.')
@minLength(3)
param adminUsername string = 'admin'

@description('Bootstrap local admin password for first login. You are forced to change it after first sign-in.')
@secure()
@minLength(12)
param adminPassword string

@description('PostgreSQL administrator username.')
@minLength(3)
@maxLength(63)
param postgresAdminLogin string = 'vmschedadmin'

@description('Auto-generated PostgreSQL administrator password. Leave unchanged unless you need to supply your own.')
@secure()
@minLength(16)
param postgresAdminPassword string = 'Vmsch!${uniqueString(subscription().id, resourceGroup().id, appName)}2026'

@description('PostgreSQL database name used by the app.')
@minLength(1)
@maxLength(63)
param postgresDatabaseName string = 'azureops'

@description('PostgreSQL Flexible Server SKU. B1ms is the lowest-cost managed option for this template.')
param postgresSkuName string = 'Standard_B1ms'

@description('Container CPU cores.')
param containerCpu string = '0.5'

@description('Container memory allocation.')
@allowed([
  '1Gi'
  '2Gi'
])
param containerMemory string = '1Gi'

// ---------------------------------------------------------------------------------------------
// Azure action gates. BOTH default to false so a fresh deployment can never start or stop a real
// machine until an operator deliberately turns it on. Every scheduled wave runs against the
// deterministic mock adapter until then. Each gate is also ANDed with a per-tenant permission
// inside the app, so enabling one here is necessary but not sufficient.
@description('Allow the scheduler to start REAL Azure virtual machines. Leave false to run every wave in mock mode until you have reviewed the estate.')
param enableRealAzureStarts bool = false

@description('Allow the scheduler to stop REAL Azure virtual machines. Deliberately separate from starts: a wrong start costs money, a wrong stop causes an outage.')
param enableRealAzureStops bool = false

// ---------------------------------------------------------------------------------------------
// Edge-level IP restrictions (optional). Empty = unchanged behaviour: ingress accepts traffic
// from anywhere. Listing CIDRs here filters at the Container Apps ingress, BEFORE a request ever
// reaches the container, and cannot be spoofed with a forwarded header. The app's own IP access
// list (Access control -> IP access) is the fine-grained, UI-editable layer on top; keep this one
// coarse, because recovering from a mistake here needs the Azure control plane:
//   az containerapp ingress access-restriction remove -n <app> -g <rg> --rule-name <name>
@description('Optional. Source CIDRs allowed to reach the app at the ingress. Leave empty to accept traffic from anywhere. Example: ["203.0.113.0/24"].')
param allowedClientIps array = []

@description('Optional. Break-glass CIDRs the application always allows, whatever its own IP access list says. Used to recover from a lock-out without touching the database.')
param ipAllowlistBootstrap string = ''

// ---------------------------------------------------------------------------------------------
// Private networking (optional). Choosing "Yes" injects the Container Apps Environment into a
// VNet and puts BOTH the storage account and the PostgreSQL Flexible Server behind Private
// Endpoints (no public access to either). NOTE: this is a CREATE-TIME choice — a Container Apps
// Environment's VNet configuration and the database's connectivity are fixed at create time, so
// an existing "No" deployment cannot be flipped to "Yes" in place; it must be redeployed.
@description('Deploy backing storage AND PostgreSQL behind Private Endpoints inside a VNet (Yes) or use the simple public deployment (No). Create-time choice; cannot be toggled later.')
@allowed([
  'No'
  'Yes'
])
param privateNetworking string = 'No'

@description('VNet address space (CIDR), used only when Private networking = Yes. Pick a range that does not overlap your existing networks.')
param vnetAddressSpace string = '10.44.0.0/22'

@description('Infrastructure subnet (CIDR) for the Container Apps Environment. Must be at least a /23 and inside the VNet address space. Used only when Private networking = Yes.')
param infraSubnetPrefix string = '10.44.0.0/23'

@description('Private Endpoint subnet (CIDR) for the storage and PostgreSQL private endpoints. Must be inside the VNet address space and not overlap the infrastructure subnet. Used only when Private networking = Yes.')
param privateEndpointSubnetPrefix string = '10.44.2.0/27'

var isPrivate = privateNetworking == 'Yes'

var normalizedAppName = toLower(appName)
var unique = uniqueString(resourceGroup().id, normalizedAppName)
var compactAppName = replace(normalizedAppName, '-', '')
var namePrefix = substring(compactAppName, 0, min(length(compactAppName), 14))
var workspaceName = '${namePrefix}-law-${unique}'
var environmentName = '${namePrefix}-env-${unique}'
var containerAppName = '${namePrefix}-app-${unique}'
var storageAccountName = toLower(replace('vmsched${unique}', '-', ''))
var fileShareName = 'appdata'
var managedEnvStorageName = 'appdata'
var postgresServerName = '${namePrefix}-pg-${unique}'
var databaseUrl = 'postgresql+asyncpg://${postgresAdminLogin}:${postgresAdminPassword}@${postgres.properties.fullyQualifiedDomainName}:5432/${postgresDatabaseName}?ssl=require'

// Private-networking resource names + subnet resource IDs (only materialised when isPrivate).
var vnetName = '${namePrefix}-vnet-${unique}'
var infraSubnetName = 'snet-infra'
var peSubnetName = 'snet-pe'
var infraSubnetId = resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, infraSubnetName)
var peSubnetId = resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, peSubnetName)
var filePrivateDnsZoneName = 'privatelink.file.${environment().suffixes.storage}'
var storageFilePeName = '${storageAccountName}-file-pe'
// Fixed for Azure public cloud; sovereign clouds use a different zone name (documented limitation).
var postgresPrivateDnsZoneName = 'privatelink.postgres.database.azure.com'
var postgresPeName = '${postgresServerName}-pe'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

// VNet for private networking. Two subnets:
//  - snet-infra: delegated to Microsoft.App/environments, hosts the VNet-injected Container Apps
//    Environment. Container Apps requires this subnet to be at least a /23.
//  - snet-pe: holds the Private Endpoint NICs; PE network policies disabled so they can be created.
resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = if (isPrivate) {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressSpace
      ]
    }
    subnets: [
      {
        name: infraSubnetName
        properties: {
          addressPrefix: infraSubnetPrefix
          delegations: [
            {
              name: 'aca-delegation'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: peSubnetName
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    // VNet injection only in private mode. resourceId() creates no implicit dependency, so the
    // environment's dependsOn (below) waits for the VNet explicitly when private.
    vnetConfiguration: isPrivate ? {
      infrastructureSubnetId: infraSubnetId
    } : null
  }
  dependsOn: isPrivate ? [
    vnet
  ] : []
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    // The Container Apps Azure Files CSI driver authenticates with the account key, so shared-key
    // access must stay enabled even in private mode.
    allowSharedKeyAccess: true
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: isPrivate ? 'Disabled' : 'Enabled'
    networkAcls: isPrivate ? {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    } : {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: fileShareName
  properties: {
    shareQuota: 16
  }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: containerEnv
  name: managedEnvStorageName
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: fileShareName
      accessMode: 'ReadWrite'
    }
  }
  dependsOn: [
    fileShare
  ]
}

resource fileDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = if (isPrivate) {
  name: filePrivateDnsZoneName
  location: 'global'
}

resource fileDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = if (isPrivate) {
  parent: fileDnsZone
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource storageFilePe 'Microsoft.Network/privateEndpoints@2023-11-01' = if (isPrivate) {
  name: storageFilePeName
  location: location
  properties: {
    subnet: {
      id: peSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'file'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: [
            'file'
          ]
        }
      }
    ]
  }
  dependsOn: [
    vnet
  ]
}

resource storageFilePeDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = if (isPrivate) {
  parent: storageFilePe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'file'
        properties: {
          privateDnsZoneId: fileDnsZone.id
        }
      }
    ]
  }
  dependsOn: [
    fileDnsZoneLink
  ]
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresServerName
  location: location
  sku: {
    name: postgresSkuName
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    // In private mode the server is reachable ONLY through its Private Endpoint; in public mode it
    // keeps TLS-guarded public access restricted by the AllowAzureServices rule below.
    network: {
      publicNetworkAccess: isPrivate ? 'Disabled' : 'Enabled'
    }
  }
}

resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: postgresDatabaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// Firewall rules are a public-access construct — only meaningful in public mode.
resource allowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (!isPrivate) {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource postgresDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = if (isPrivate) {
  name: postgresPrivateDnsZoneName
  location: 'global'
}

resource postgresDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = if (isPrivate) {
  parent: postgresDnsZone
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource postgresPe 'Microsoft.Network/privateEndpoints@2023-11-01' = if (isPrivate) {
  name: postgresPeName
  location: location
  properties: {
    subnet: {
      id: peSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'postgres'
        properties: {
          privateLinkServiceId: postgres.id
          groupIds: [
            'postgresqlServer'
          ]
        }
      }
    ]
  }
  dependsOn: [
    vnet
  ]
}

resource postgresPeDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = if (isPrivate) {
  parent: postgresPe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'postgres'
        properties: {
          privateDnsZoneId: postgresDnsZone.id
        }
      }
    ]
  }
  dependsOn: [
    postgresDnsZoneLink
  ]
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  // System-assigned identity so the app can authenticate to Azure Resource Manager without any
  // stored credential. Add an "Azure tenant" in the app with auth method "default_chain", then
  // grant THIS identity the rights it needs on the subscriptions or resource groups it manages
  // (reader, plus start/deallocate on the virtual machines you want it to schedule).
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: containerEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        // Empty stays absent so the default (accept everything) is preserved. A non-empty list is
        // an implicit deny for everything else, which is exactly the semantics we want.
        ipSecurityRestrictions: [for (cidr, index) in allowedClientIps: {
          name: 'allow-${index}'
          description: 'Allowed source range'
          ipAddressRange: cidr
          action: 'Allow'
        }]
      }
      secrets: usePrivateRegistry ? [
        {
          name: 'database-url'
          value: databaseUrl
        }
        {
          name: 'admin-password'
          value: adminPassword
        }
        {
          name: 'fernet-key'
          value: '${guid(subscription().id, resourceGroup().id, appName, 'fernet')}${guid(resourceGroup().id, appName, 'fernet2')}'
        }
        {
          name: 'registry-password'
          value: registryPassword
        }
      ] : [
        {
          name: 'database-url'
          value: databaseUrl
        }
        {
          name: 'admin-password'
          value: adminPassword
        }
        {
          // Encrypts Azure credentials at rest. Held as a Container App secret rather than only on
          // the file share, so the share alone never discloses the stored client secrets.
          // Deterministic in the deployment's own identifiers, so a redeploy derives the same key
          // and previously stored credentials stay readable. The app accepts any passphrase here
          // and stretches it with PBKDF2.
          name: 'fernet-key'
          value: '${guid(subscription().id, resourceGroup().id, appName, 'fernet')}${guid(resourceGroup().id, appName, 'fernet2')}'
        }
      ]
      registries: usePrivateRegistry ? [
        {
          server: registryServer
          username: registryUsername
          passwordSecretRef: 'registry-password'
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'azureops'
          image: containerImage
          env: [
            {
              name: 'BOOTSTRAP_ADMIN_USERNAME'
              value: adminUsername
            }
            {
              name: 'BOOTSTRAP_ADMIN_PASSWORD'
              secretRef: 'admin-password'
            }
            {
              name: 'DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'FERNET_KEY'
              secretRef: 'fernet-key'
            }
            {
              name: 'DATA_DIR'
              value: '/app/.data'
            }
            {
              name: 'ENVIRONMENT'
              value: 'production'
            }
            {
              // Container Apps ingress terminates TLS and is the only peer this container talks to,
              // so its forwarded headers are trustworthy. They carry the real client IP for the
              // per-IP sign-in throttle and the original scheme for HSTS.
              name: 'TRUST_FORWARDED_HEADERS'
              value: 'true'
            }
            {
              // Container Apps ingress is exactly one hop, so the real client is the last entry it
              // appended to X-Forwarded-For. Raise this if you put anything else in front (Front
              // Door, an nginx sidecar), or every address-based decision reads the wrong value.
              name: 'FORWARDED_HOPS'
              value: '1'
            }
            {
              // Break-glass for the app's IP access list. Environment-only on purpose: an
              // administrator locked out of the UI restores access by setting this and restarting,
              // with no database surgery.
              name: 'IP_ALLOWLIST_BOOTSTRAP'
              value: ipAllowlistBootstrap
            }
            // Both safety gates ship off. Turn them on only after reviewing the estate; each is
            // still ANDed with the per-tenant permission set inside the app.
            {
              name: 'ENABLE_REAL_AZURE_STARTS'
              value: string(enableRealAzureStarts)
            }
            {
              name: 'ENABLE_REAL_AZURE_STOPS'
              value: string(enableRealAzureStops)
            }
            {
              name: 'APP_BASE_URL'
              value: 'https://${containerAppName}.${containerEnv.properties.defaultDomain}'
            }
            {
              name: 'ALLOWED_RETURN_ORIGINS'
              value: 'https://${containerAppName}.${containerEnv.properties.defaultDomain}'
            }
          ]
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
          volumeMounts: [
            {
              volumeName: 'appdata'
              mountPath: '/app/.data'
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
              }
              initialDelaySeconds: 20
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/readyz'
                port: 8000
              }
              initialDelaySeconds: 10
              periodSeconds: 15
              failureThreshold: 10
            }
          ]
        }
      ]
      scale: {
        // PINNED to one replica. The scheduler runs in-process and claims work with database
        // leases sized for a single instance; a second replica would risk firing a wave twice.
        // Do not add scale rules without moving the scheduler to a shared, fenced lock.
        minReplicas: 1
        maxReplicas: 1
      }
      volumes: [
        {
          name: 'appdata'
          storageType: 'AzureFile'
          storageName: managedEnvStorageName
        }
      ]
    }
  }
  // In private mode the app must wait for the Postgres private endpoint's DNS so its first
  // connection resolves to the private IP; in public mode it waits for the firewall rule instead.
  dependsOn: isPrivate ? [
    postgresDatabase
    envStorage
    storageFilePeDnsGroup
    postgresPeDnsGroup
  ] : [
    postgresDatabase
    allowAzureServices
    envStorage
  ]
}

output applicationUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output containerAppName string = containerApp.name
output containerAppPrincipalId string = containerApp.identity.principalId
output postgresServerName string = postgres.name
output storageAccountName string = storage.name
output privateNetworking string = privateNetworking
output vnetName string = isPrivate ? vnetName : ''
output postgresPrivateEndpoint string = isPrivate ? postgresPeName : ''
