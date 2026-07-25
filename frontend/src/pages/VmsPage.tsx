import { PageHeader } from '../components/Ui'
import { VmTable } from '../components/VmTable'

/** Global virtual machine inventory across every application and ring. */
export function VmsPage() {
  return <>
    <PageHeader title="Virtual machines" description="Every VM in the inventory, with its group, effective Azure connection and overrides." />
    <VmTable title="Inventory" description="Filter by group, state or tenant. Select rows for bulk actions." />
  </>
}
