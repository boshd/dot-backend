import { useState } from "react";
import {
  AppShell,
  Checkbox,
  Item,
  List,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@dot/ui";

export default function CompactChecklist() {
  const [tab, setTab] = useState("prep");
  const [done, setDone] = useState(false);
  return (
    <AppShell title="Trip prep" density="compact">
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="prep">Prep</TabsTrigger>
          <TabsTrigger value="plan">Plan</TabsTrigger>
        </TabsList>
        <TabsContent value="prep">
          <List>
            <Item
              leading={<Checkbox label="Packed" checked={done} onCheckedChange={setDone} />}
              title="Check passport validity"
              detail="Kareem"
              meta="Open"
            />
          </List>
        </TabsContent>
        <TabsContent value="plan">empty</TabsContent>
      </Tabs>
    </AppShell>
  );
}
