from pychfpga.fpga_firmware.chfpga.ct_health import summarize_health_passes

LINK = '[slot 2] BP pcb lane 11 (fed by slot 10 lane 13)'


def test_clean_soak_summarizes_to_empty():
    assert summarize_health_passes({n: [] for n in range(1, 61)}, 60) == ''


def test_flaky_link_is_grouped_by_location_across_passes():
    passes = {n: [] for n in range(1, 61)}
    passes[7] = [f'{LINK}: 2 CRC/frame errors on the backplane link',
                 '[slot 2] CROSSBAR2 lane 2: PACKET_ERROR_CTR=2 (CRC/length/alignment errors)']
    passes[31] = [f'{LINK}: 1 CRC/frame errors on the backplane link']

    summary = summarize_health_passes(passes, 60)

    lines = summary.splitlines()
    assert lines[0] == '2 location(s) reported problems in 2 of 60 health passes:'
    assert lines[1] == (f'  {LINK}: 2/60 passes (7, 31); '
                        'first: 2 CRC/frame errors on the backplane link')
    assert lines[2].startswith('  [slot 2] CROSSBAR2 lane 2: 1/60 passes (7); first: ')


def test_long_pass_lists_are_truncated():
    passes = {n: ['[slot 5] CROSSBAR3 lane 0: PACKET_ERROR_CTR=1 (CRC/length/alignment errors)']
              for n in range(1, 13)}

    summary = summarize_health_passes(passes, 12)

    assert '12/12 passes (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, ...)' in summary


def test_ber_equivalent_matches_the_qc_limit_of_one_error_per_16_s():
    from pychfpga.fpga_firmware.chfpga.ct_health import ber_equivalent

    assert ber_equivalent(1, 16) == 1e-10
    assert ber_equivalent(112, 1800) < 1e-10 <= ber_equivalent(113, 1800)


def test_link_error_rates_report_clean_borderline_failing_and_saturated_links():
    from pychfpga.fpga_firmware.chfpga.ct_health import summarize_link_error_rates

    totals = {(s, lane): (1, 1, 0, 1800.0, False) for s in range(10, 16) for lane in range(1, 16)}
    totals[(2, 11)] = (10, 13, 90, 1800.0, False)      # borderline pass
    totals[(5, 4)] = (15, 15, 200, 1800.0, False)      # over the limit
    totals[(9, 1)] = (4, 2, 65535, 1800.0, True)       # counter saturated

    failing, lines = summarize_link_error_rates(totals, 1e-10)

    assert sorted(failing) == [(5, 4), (9, 1)]
    assert lines[0] == '93 backplane links: 3 with errors, 2 at or over BER 1e-10, 90 error-free'
    assert lines[2] == ('[slot 5] BP pcb lane 4 (fed by slot 15 lane 15): 200 CRC/frame errors in '
                        '1800 s, BER-equivalent 1.8e-10 - OVER THE LIMIT')
    assert lines[3] == ('[slot 2] BP pcb lane 11 (fed by slot 10 lane 13): 90 CRC/frame errors in '
                        '1800 s, BER-equivalent 8.0e-11')
    assert 'counter saturated' in lines[1] and lines[1].endswith('OVER THE LIMIT')


def test_clean_links_report_no_failures():
    from pychfpga.fpga_firmware.chfpga.ct_health import summarize_link_error_rates

    failing, lines = summarize_link_error_rates({(2, 1): (3, 4, 0, 60.0, False)})

    assert failing == []
    assert lines == ['1 backplane links: 0 with errors, 0 at or over BER 1e-10, 1 error-free']
