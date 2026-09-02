import { redirect } from "next/navigation";

/** The runs list is the honest front door: it says what the tool has done. */
export default function Home() {
  redirect("/jobs");
}
