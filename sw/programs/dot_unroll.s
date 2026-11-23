# dot_unroll.s - dot product with the loop unrolled 4 times
#
# Same answer as dot.s:  s3 = A[0]*B[0] + ... + A[N-1]*B[N-1]
# Each pass handles 4 vectors. The order is chosen for the pipelined core:
#   * all 8 loads first, so no VDOT waits for a load
#   * the 4 VDOTs write 4 different scalar registers (s4..s7), and the ADDs
#     that use them come later, so no ADD waits for a lane sum
#
#   dot.s         6 instructions per vector, plus 2 waits on the pipelined core
#   dot_unroll.s  18 instructions per 4 vectors, no waits
#
# Constants: A, B (array addresses), ITER (= N / LANES, a multiple of 4)

        ADDI s1, s0, 0          # s1 = word index
        ADDI s2, s0, ITER       # s2 = where to stop
        ADDI s3, s0, 0          # s3 = running total
loop:   VLD  v0, A(s1)
        VLD  v1, B(s1)
        VLD  v2, A+1(s1)
        VLD  v3, B+1(s1)
        VLD  v4, A+2(s1)
        VLD  v5, B+2(s1)
        VLD  v6, A+3(s1)
        VLD  v7, B+3(s1)
        VDOT s4, v0, v1         # 4 partial dot products
        VDOT s5, v2, v3
        VDOT s6, v4, v5
        VDOT s7, v6, v7
        ADD  s3, s3, s4         # s4 was ready 3 instructions ago
        ADD  s3, s3, s5
        ADD  s3, s3, s6
        ADD  s3, s3, s7
        ADDI s1, s1, 4
        BNE  s1, s2, loop
        HALT
