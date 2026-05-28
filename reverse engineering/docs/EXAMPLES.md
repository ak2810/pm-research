# EXAMPLES

Example values for documentation and test fixtures only. These are NOT
production values — every production value is derived from live data.

---

## Example token_id (uint256 decimal string)

```
"85820491405070503157833602237286422627039369979142976656076036182129921475920"
```

## Example ohanism proxy wallet

```
0x89b5cdaaa4866c1e738406712012a630b4078beb
```

## Example condition_id / market hex

```
0x525bc811d0ef8672e26e903371ebbfbe3a6d24bf4c55aac41ddd74499d09ffa3
```

## Example slug (5m market)

```
btc-updown-5m-1779783300
```

## Example slug (hourly market)

```
bitcoin-up-or-down-may-27-2026-4am-et
```

## Example block timestamp (ISO + int64 ns)

```
2026-05-27T08:00:01Z  →  1748332801000000000
```

## Example price as Decimal string

```
"0.520000"   # 52% probability Up
"0.014000"   # 1.4% probability Up (deep OTM)
```

## Example fee calculation at p=0.5

```
fee_rate = 0.07
price = 0.5
fee_per_unit = 0.07 * min(0.5, 0.5) = 0.07 * 0.5 = 0.035
rebate_per_unit = 0.2 * 0.035 = 0.007
```

## Example builder (zero = direct submission)

```
"0000000000000000000000000000000000000000000000000000000000000000"
```
