# vadd_unroll.s - vadd with the loop unrolled 4 times:  C[i] = A[i] + B[i]
#
# Same answer as vadd.s, but each pass handles 4 vectors instead of 1:
#   * the loop bookkeeping (ADDI + BNE) is paid once per 4 vectors, not per vector
#   * all 8 loads come first, so by the time each VADD runs its inputs were
#     loaded several instructions earlier: the pipelined core never has to
#     wait for a load (a "load-use stall")
#
#   vadd.s        6 instructions per vector  (2 of them loop bookkeeping)
#   vadd_unroll.s 18 instructions per 4 vectors = 4.5 per vector
#
# Constants: A, B, C (array addresses), ITER (= N / LANES, a multiple of 4)

        ADDI s1, s0, 0          # s1 = word index
        ADDI s2, s0, ITER       # s2 = where to stop
loop:   VLD  v0, A(s1)          # load 4 vectors of A and 4 of B
        VLD  v1, B(s1)
        VLD  v2, A+1(s1)
        VLD  v3, B+1(s1)
        VLD  v4, A+2(s1)
        VLD  v5, B+2(s1)
        VLD  v6, A+3(s1)
        VLD  v7, B+3(s1)
        VADD v0, v0, v1         # 4 additions, each on registers loaded long ago
        VADD v2, v2, v3
        VADD v4, v4, v5
        VADD v6, v6, v7
        VST  v0, C(s1)          # store the 4 results
        VST  v2, C+1(s1)
        VST  v4, C+2(s1)
        VST  v6, C+3(s1)
        ADDI s1, s1, 4          # 4 vectors done
        BNE  s1, s2, loop
        HALT
