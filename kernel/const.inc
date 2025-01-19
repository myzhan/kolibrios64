struct DQ
        union
                lo    dd ?
                hi_be dd ?      ; big endian
        ends
        union
                hi    dd ?
                lo_be dd ?
        ends
ends