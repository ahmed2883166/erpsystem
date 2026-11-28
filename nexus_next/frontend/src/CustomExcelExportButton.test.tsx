// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import CustomExcelExportButton from "./CustomExcelExportButton";

const fetchMock = vi.fn();

function jsonResponse(data: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
  };
}

describe("CustomExcelExportButton", () => {
  beforeEach(() => {
    localStorage.clear();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", Object.assign(URL, {
      createObjectURL: vi.fn(() => "blob:custom-export"),
      revokeObjectURL: vi.fn(),
    }));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("loads fields, reorders and saves a template, previews before download", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({
        fields: [
          { key: "name", label: "اسم العميل" },
          { key: "code", label: "كود العميل" },
        ],
      }))
      .mockResolvedValueOnce(jsonResponse({
        headers: ["كود العميل", "اسم العميل"],
        total_rows: 1,
        preview_rows: [["C-01", "عميل تجريبي"]],
      }))
      .mockResolvedValueOnce({
        ok: true,
        blob: async () => new Blob(["xlsx"]),
      });

    render(<CustomExcelExportButton resource="customers" companyId={7} branchId={3} />);
    fireEvent.click(screen.getByRole("button", { name: "تصدير Excel مخصص" }));

    const nameField = await screen.findByLabelText("اسم العميل");
    const codeField = screen.getByLabelText("كود العميل");
    expect(nameField).toBeChecked();
    expect(codeField).toBeChecked();

    fireEvent.click(screen.getAllByRole("button", { name: "تحريك لأسفل" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "حفظ قالب الأعمدة" }));
    expect(JSON.parse(localStorage.getItem("admrail-export-template:customers") || "[]"))
      .toEqual(["code", "name"]);

    const downloadButton = screen.getByRole("button", { name: "تنزيل Excel" });
    expect(downloadButton).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "معاينة / اختبار" }));
    await waitFor(() => expect(screen.getByText(/المعاينة:/)).toBeInTheDocument());
    expect(screen.getByText("عميل تجريبي")).toBeInTheDocument();
    expect(downloadButton).toBeEnabled();

    const previewUrl = new URL(String(fetchMock.mock.calls[1][0]), "http://localhost");
    expect(previewUrl.searchParams.get("fields")).toBe("code,name");
    expect(previewUrl.searchParams.get("branch")).toBe("3");
    expect(previewUrl.searchParams.get("preview")).toBe("1");

    fireEvent.click(downloadButton);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const downloadUrl = new URL(String(fetchMock.mock.calls[2][0]), "http://localhost");
    expect(downloadUrl.searchParams.get("fields")).toBe("code,name");
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled();
  });

  it("does not permit download until preview succeeds and invalidates preview when columns change", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({
        fields: [{ key: "name", label: "اسم العميل" }],
      }))
      .mockResolvedValueOnce(jsonResponse({
        headers: ["اسم العميل"], total_rows: 0, preview_rows: [],
      }));

    render(<CustomExcelExportButton resource="customers" companyId={7} />);
    fireEvent.click(screen.getByRole("button", { name: "تصدير Excel مخصص" }));
    await screen.findByLabelText("اسم العميل");

    const downloadButton = screen.getByRole("button", { name: "تنزيل Excel" });
    expect(downloadButton).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "معاينة / اختبار" }));
    await waitFor(() => expect(downloadButton).toBeEnabled());

    fireEvent.click(screen.getByLabelText("اسم العميل"));
    expect(downloadButton).toBeDisabled();
  });

  it("shows a permission error when the fields endpoint rejects access", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}, 403));
    render(<CustomExcelExportButton resource="employees" companyId={7} />);
    fireEvent.click(screen.getByRole("button", { name: "تصدير Excel مخصص" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("ليس لديك صلاحية التصدير.");
  });
});
