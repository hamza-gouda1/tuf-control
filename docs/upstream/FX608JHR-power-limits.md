# Upstream request: power limits for ASUS TUF Gaming F16 FX608JHR

Draft text, ready to adapt. Nothing here has been sent. The numeric limits are
deliberately left blank: they must come from measured or vendor-provided
values, never from guesses.

## Where to send it

- asus-linux issue tracker (asusctl / asusd): https://gitlab.com/asus-linux/asusctl/-/issues
- Linux platform-drivers-x86 mailing list (driver `asus-armoury`), with the
  maintainers of that driver on Cc (see `scripts/get_maintainer.pl
  drivers/platform/x86/asus-armoury.c` in the kernel tree you test against).

## Issue text

**Title:** asus-armoury: no power-limit table entry for ASUS TUF Gaming F16 FX608JHR (all PPT/NV attributes return ENODEV)

**Hardware**

- Product: ASUS TUF Gaming F16 FX608JHR_FX608JHR
- DMI board name: `FX608JHR`
- BIOS: `FX608JHR.309` (2025-11-03)
- CPU / GPU: Intel Core i7-14650HX, NVIDIA GeForce RTX 5050 Laptop GPU
  (hybrid graphics, supergfxd 5.2.7)

**Software**

- Kernel: 7.0.13-3-liquorix-amd64 (`CONFIG_ASUS_ARMOURY=m`, `CONFIG_ASUS_WMI=m`,
  `CONFIG_ASUS_NB_WMI=m`; modules `asus_armoury`, `asus_wmi`, `asus_nb_wmi` loaded)
- asusctl / asusd: 6.3.5

**What happens**

The attribute directories exist under
`/sys/class/firmware-attributes/asus-armoury/attributes/` for `ppt_pl1_spl`,
`ppt_pl2_sppt`, `nv_dynamic_boost`, `nv_temp_target`, `nv_tgp` and
`nv_base_tgp`. `display_name` and `type` can be read. Reading `current_value`,
`min_value`, `max_value` or `default_value` of the PPT/NV attributes fails with
`No such device` (ENODEV). `ppt_pl3_fppt` is not created at all. asusd logs
`Failed to read: Io(Os { code: 19, kind: Uncategorized, message: "No such device" })`
and reports -1 for min, max and default over D-Bus.

`charge_mode`, `dgpu_disable`, `gpu_mux_mode`, `panel_overdrive` and
`screen_auto_brightness` work normally.

**Analysis**

The driver keys its per-model limits table on the DMI board name. A board
without an entry gets no limits structure, so the show handlers return ENODEV.
`FX608JHR` has no entry in the tree I could inspect (a sibling board,
`FX608JPR`, was proposed in a June 2026 series; the match is exact so it does
not cover this model).

Rechecked 2026-11-01 against the upstream [asus-armoury model table](https://github.com/torvalds/linux/blob/master/drivers/platform/x86/asus-armoury.h):
no `FX608JHR` match was present. This is a dated observation; retest after a kernel update.

**Request**

Please add a power-limit entry for board `FX608JHR`, or tell me which
measurements you need from this machine to produce one (I can run read-only
probes and, on request, tests on this hardware). Values should come from ASUS
reference data or the BIOS, not from another model.

## Patch sketch (limits intentionally unfilled)

The exact field names must follow the current `asus-armoury.h` of the tree it
is sent against. Structure only:

```c
{
	.matches = {
		DMI_MATCH(DMI_BOARD_NAME, "FX608JHR"),
	},
	.driver_data = &(struct power_data) {
		.ac_data = &(struct power_limits) {
			.ppt_pl1_spl_min = /* from vendor data */,
			.ppt_pl1_spl_max = /* from vendor data */,
			.ppt_pl2_sppt_min = /* from vendor data */,
			.ppt_pl2_sppt_max = /* from vendor data */,
			.nv_dynamic_boost_min = /* from vendor data */,
			.nv_dynamic_boost_max = /* from vendor data */,
			.nv_temp_target_min = /* from vendor data */,
			.nv_temp_target_max = /* from vendor data */,
		},
		/* .dc_data likewise, if the model has separate battery limits */
	},
},
```

Signed-off-by and the commit message belong to whoever submits it.

## Evidence to attach

```bash
cat /sys/class/dmi/id/{board_name,bios_version,bios_date,product_name}
uname -r
asusctl --version
for a in ppt_pl1_spl ppt_pl2_sppt nv_dynamic_boost nv_temp_target nv_tgp nv_base_tgp; do
  echo "== $a"; ls /sys/class/firmware-attributes/asus-armoury/attributes/$a
  cat /sys/class/firmware-attributes/asus-armoury/attributes/$a/display_name
  cat /sys/class/firmware-attributes/asus-armoury/attributes/$a/current_value
done
journalctl -u asusd --no-pager -n 80
busctl --system introspect xyz.ljones.Asusd /xyz/ljones/asus_armoury/ppt_pl1_spl
```

## How tuf-control behaves until this is fixed

The Performance page does not show sliders when the firmware reports no
minimum and maximum. It explains why, and it never writes a guessed value.
Once the kernel exposes real limits, the same page shows them with the
firmware's own bounds as hard limits, with no code change.
