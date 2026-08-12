import React, { useState } from "react";
import { AppShell, Button, Checkbox, Input, Stack } from "@dot/ui";

export default function CottageExpenseSplitter() {
  const [description, setDescription] = useState("");
  const [splitEveryone, setSplitEveryone] = useState(true);

  return (
    <AppShell title="Cottage expenses">
      <Stack gap="large">
        <Input
          label="Expense"
          value={description}
          onValueChange={setDescription}
        />
        <Checkbox
          label="Split between everyone"
          checked={splitEveryone}
          onCheckedChange={setSplitEveryone}
        />
        <Checkbox
          label="Include me"
          checked={splitEveryone}
          onValueChange={setSplitEveryone}
        />
        <Button size="medium">Add expense</Button>
      </Stack>
    </AppShell>
  );
}
